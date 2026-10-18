"""Stage and edit a CV without changing its original template; verify its PDF."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

TARGET_PREFIXES = (r"\item Created an internal portal", r"\item Used Gradient Boosting")
INLINE_COMMANDS = {"item", "textbf", "textit", "emph", "texttt", "textnormal", "textrm",
                   "textasciitilde", "textasciicircum", "LaTeX", "TeX"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    """Replace a file only after the complete new contents have been flushed."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _escaped(text: str, position: int) -> bool:
    count = 0
    position -= 1
    while position >= 0 and text[position] == "\\":
        count += 1
        position -= 1
    return count % 2 == 1


def _active(line: str) -> str:
    for index, char in enumerate(line):
        if char == "%" and not _escaped(line, index):
            return line[:index]
    return line


@dataclass(frozen=True)
class Targets:
    indices: tuple[int, int]
    third_bullet: str


def locate_targets(source: str, first: int, last: int) -> Targets:
    lines = source.splitlines(keepends=True)
    if first < 1 or last < first or last > len(lines):
        raise ValueError("LaTeX line range is outside the source")
    indices = []
    for prefix in TARGET_PREFIXES:
        matches = [i for i, line in enumerate(lines) if _active(line).lstrip().startswith(prefix)]
        if len(matches) != 1 or not first <= matches[0] + 1 <= last:
            raise ValueError(f"Expected one '{prefix}' bullet inside lines {first}–{last}")
        indices.append(matches[0])
    if indices[0] >= indices[1]:
        raise ValueError("The two NOMOS bullets are not in the expected order")
    active_source = "".join(_active(line) + ("\n" if not _active(line).endswith("\n") else "")
                            for line in lines)
    if r"\begin{highlights}" in active_source:
        wrapper = re.search(
            r"\\newenvironment\{highlights\}\s*\{\s*\\begin\{itemize\}.*?\}\s*"
            r"\{\s*\\end\{itemize\}\s*\}", active_source, re.DOTALL,
        )
        if not wrapper:
            raise ValueError("The highlights wrapper is not a verified itemize environment")
    stack = []
    target_lists = []
    for i, line in enumerate(lines):
        for event, name in re.findall(r"\\(begin|end)\{([\w*]+)\}", _active(line)):
            if event == "begin":
                stack.append((name, i))
            elif stack and stack[-1][0] == name:
                stack.pop()
            elif stack:
                raise ValueError("Unbalanced LaTeX environments before the target list")
        if i in indices:
            lists = [entry for entry in stack if entry[0] in {"itemize", "highlights"}]
            if not lists:
                raise ValueError("Replacement bullet is outside an itemize list")
            target_lists.append(lists[-1])
    if len(target_lists) != 2 or target_lists[0] != target_lists[1]:
        raise ValueError("The two replacement bullets are not in the same list")
    environment, start = target_lists[0]
    end = next((i for i in range(indices[1] + 1, len(lines))
                if rf"\end{{{environment}}}" in _active(lines[i])), None)
    if end is None:
        raise ValueError("The target itemize list has no closing command")
    preceding = "".join(_active(line) for line in lines[:start])
    preceding = re.split(r"\\end\{(?:itemize|highlights)\}", preceding)[-1]
    if not re.search(r"\bNOMOS\b", preceding[-1200:], re.IGNORECASE):
        raise ValueError("The selected list does not belong to the NOMOS employment entry")
    third = next((_active(line).strip() for line in lines[indices[1] + 1:end]
                  if _active(line).lstrip().startswith(r"\item ")), "")
    if not third:
        raise ValueError("The unchanged third NOMOS bullet is missing")
    return Targets(tuple(indices), third)


def parse_bullets(answer: str) -> tuple[str, str]:
    """Extract two physical item lines, allowing fences and standalone comments."""
    # Comments may label replacement slots, but must never become CV content.
    # Keep inline comments intact so an unescaped percentage still fails below.
    answer = "\n".join(line for line in answer.splitlines() if not line.lstrip().startswith("%"))
    fence_pattern = r"(?m)^[ \t]*```[^\n]*\n(.*?)^[ \t]*```[ \t]*(?:\n|$)"
    fences = re.findall(fence_pattern, answer, re.DOTALL)
    outside = re.sub(fence_pattern, "", answer, flags=re.DOTALL)
    if re.search(r"(?m)^[ \t]*```", outside):
        raise ValueError("Unclosed or malformed LaTeX code fence")
    if fences:
        candidates = [block for block in fences if re.search(r"(?m)^\s*\\item\b", block)]
        lines = [line.strip() for block in candidates for line in block.splitlines() if line.strip()]
        if not all(line.startswith(r"\item ") for line in lines):
            raise ValueError("Each of the two LaTeX bullets must occupy exactly one physical line")
        # Separate one-bullet blocks are valid; additional alternatives are not.
        if len(lines) != 2 or len(re.findall(r"(?m)^\s*\\item\b", answer)) != 2:
            raise ValueError("Expected exactly two LaTeX bullets")
    else:
        all_lines = answer.splitlines()
        positions = [i for i, line in enumerate(all_lines) if line.strip().startswith(r"\item")]
        if len(positions) != 2:
            raise ValueError("Expected exactly two LaTeX bullets")
        for i in positions:
            if i + 1 < len(all_lines) and all_lines[i + 1].strip() and i + 1 not in positions:
                raise ValueError("Wrapped bullet content must be returned on one physical line")
        if any(all_lines[i].strip() for i in range(positions[0] + 1, positions[1]) if i not in positions):
            raise ValueError("Unexpected content between the LaTeX bullets")
        lines = [all_lines[i].strip() for i in positions]
    for line in lines:
        if not re.match(r"^\\item\s+\S", line) or line.count(r"\item") != 1:
            raise ValueError("Each bullet must start with one nonempty \\item")
        depth = 0
        for index, char in enumerate(line):
            if not _escaped(line, index):
                if char in "%&#_$^~":
                    raise ValueError(f"Unescaped LaTeX symbol {char!r} in generated bullet")
                if char == "{":
                    depth += 1
                if char == "}":
                    depth -= 1
                    if depth < 0:
                        raise ValueError("Unbalanced LaTeX braces")
        if depth:
            raise ValueError("Unbalanced LaTeX braces")
        for command in re.findall(r"\\([A-Za-z]+|.)", line):
            if command not in INLINE_COMMANDS and command not in "%&#_${} ":
                raise ValueError(f"Unsupported LaTeX command \\{command} in generated bullet")
    return tuple(lines)


def replace_bullets(source: bytes, targets: Targets, bullets: tuple[str, str], record) -> bytes:
    lines = source.decode("utf-8").splitlines(keepends=True)
    originals = list(lines)
    slots = {}
    for index in targets.indices:
        line = lines[index]
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        slots[index] = (re.match(r"[ \t]*", line).group(), newline)
        lines[index] = newline
    if any(lines[index].strip() for index in targets.indices):
        raise ValueError("Target line clearing could not be verified")
    record("latex_clear", "SUCCESS", f"Cleared only lines {[i + 1 for i in targets.indices]} in memory")
    for index, bullet in zip(targets.indices, bullets):
        indent, newline = slots[index]
        lines[index] = indent + bullet + newline
    if len(lines) != len(originals) or any(
        line != originals[i] for i, line in enumerate(lines) if i not in targets.indices
    ):
        raise ValueError("LaTeX replacement changed another source line")
    record("latex_insert", "SUCCESS", f"Inserted one bullet per original line {[i + 1 for i in targets.indices]}")
    return "".join(lines).encode("utf-8")


def validate_archive(archive: Path) -> None:
    with zipfile.ZipFile(archive) as zipped:
        members = zipped.infolist()
        if sum(info.file_size for info in members) > 100 * 1024 * 1024:
            raise ValueError("EU archive is unexpectedly large")
        names = set()
        for info in members:
            name = PurePosixPath(info.filename)
            if (name.is_absolute() or ".." in name.parts or "\\" in info.filename
                    or ":" in info.filename or stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError("EU archive contains an unsafe member")
            if info.filename in names:
                raise ValueError("EU archive contains duplicate members")
            names.add(info.filename)
        if not {"mmayer.tex", "altacv.cls", "portrait.png"} <= names:
            raise ValueError("EU archive must contain mmayer.tex, altacv.cls, and portrait.png at its root")


def stage_template(folder: Path, source: bytes, *, uk: bool, archive: Path | None) -> Path:
    if not uk:
        validate_archive(archive)
        copied_archive = folder / f"{folder.name}.zip"
        atomic_write(copied_archive, archive.read_bytes())
        with zipfile.ZipFile(copied_archive) as zipped:
            for info in zipped.infolist():
                name = PurePosixPath(info.filename)
                if info.is_dir() or name.suffix.lower() == ".tex" or name.name in {
                    "main.pdf", "mmayer.pdf", "AndrewBodzsarCV.pdf", "main.log", "mmayer.log"
                }:
                    continue
                destination = folder.joinpath(*name.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(destination, zipped.read(info))
    path = folder / ("main.tex" if uk else "mmayer.tex")
    atomic_write(path, source)
    if list(folder.rglob("*.tex")) != [path] or path.read_bytes() != source:
        raise ValueError("Template staging did not produce exactly the selected source")
    active = "\n".join(_active(line) for line in source.decode("utf-8").splitlines())
    # A template requiring extra local TeX files conflicts with the one-source contract.
    for reference in re.findall(r"\\(?:input|include)\{([^}]+)\}", active):
        if reference != "glyphtounicode":
            raise ValueError(f"Additional TeX source dependency is not supported: {reference}")
    for reference in re.findall(r"\\(?:photoR|photoL)\{[^}]*\}\{([^}]+)\}", active):
        for photo in reference.split(","):
            if not any((folder / (photo + suffix)).is_file() for suffix in ("", ".png", ".jpg", ".pdf")):
                raise ValueError(f"Missing template photo: {photo}")
    for reference in re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", active):
        if not any((folder / (reference + suffix)).is_file() for suffix in ("", ".png", ".jpg", ".pdf", ".jpeg")):
            raise ValueError(f"Missing template graphic: {reference}")
    return path


def latex_plain_text(text: str) -> str:
    text = re.sub(r"\\item\s*", "", text)
    text = text.replace(r"\textasciitilde", "~").replace(r"\textasciicircum", "^")
    text = re.sub(r"\\(?:textbf|textit|emph|texttt|textnormal|textrm)\s*\{", "{", text)
    text = re.sub(r"\\([%&#_$])", r"\1", text)
    return text.replace("{", "").replace("}", "")


def normalized_pdf_text(text: str) -> str:
    # Ignore line wrapping, discretionary hyphens, accents, and common ligatures.
    text = unicodedata.normalize("NFKD", text).casefold()
    return "".join(char for char in text if char.isalnum())


def verify_pdf(pdf: Path, bullets: tuple[str, str], third_bullet: str,
               candidate_name: str, *, root: Path) -> dict:
    if not pdf.is_file() or pdf.stat().st_size == 0 or not pdf.read_bytes().startswith(b"%PDF-"):
        raise ValueError("Generated PDF is missing, empty, or invalid")
    info = subprocess.run(["pdfinfo", str(pdf)], cwd=root, capture_output=True, text=True, check=True, timeout=30)
    pages = re.search(r"(?m)^Pages:\s*(\d+)", info.stdout)
    if not pages or int(pages[1]) <= 0:
        raise ValueError("Generated PDF has no readable pages")
    result = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], cwd=root,
                            capture_output=True, text=True, check=True, timeout=30)
    text = result.stdout
    normalized = normalized_pdf_text(text)
    required = [candidate_name, "NOMOS", "Experience", "Education"]
    required.extend(latex_plain_text(bullet) for bullet in (*bullets, third_bullet))
    for number, expected in enumerate(required):
        if not expected or normalized_pdf_text(expected) not in normalized:
            raise ValueError(f"PDF content verification failed for expected element {number + 1}")
    return {"pages": int(pages[1]), "text": text, "sha256": sha256(pdf.read_bytes())}


def compile_resume(source: Path, *, root: Path, record, timeout: int = 180) -> Path:
    output = source.with_suffix(".pdf")
    if output.exists():
        raise ValueError("A PDF already exists before compilation; refusing stale output")
    command = ["tectonic", "-X", "compile", "--keep-logs", "--outdir", str(source.parent), str(source)]
    record("compile", "START", "Executing " + " ".join(command))
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        for stream in (exc.stdout, exc.stderr):
            if stream:
                record("compile", "OUTPUT", stream.decode(errors="replace") if isinstance(stream, bytes) else stream)
        raise ValueError("Tectonic compilation timed out") from exc
    for stream in (result.stdout, result.stderr):
        if stream:
            record("compile", "OUTPUT", stream)
    if result.returncode or not output.is_file() or output.stat().st_size == 0:
        raise ValueError(f"Tectonic compilation failed (exit {result.returncode}); see job logs")
    pdf = source.parent / "AndrewBodzsarCV.pdf"
    output.replace(pdf)
    record("compile", "SUCCESS", f"Created {pdf}")
    return pdf
