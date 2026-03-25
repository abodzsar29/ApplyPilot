#!/usr/bin/env python3
from applypilot.linkedin.search import get_linkedin_jobs
import json

config = json.load(open('config/linkedin_apply.json'))
config['max_applications'] = 5  # Test with 5 jobs
jobs = get_linkedin_jobs(config)
print(f'\nFound {len(jobs)} jobs total')
for i, job in enumerate(jobs, 1):
    print(f'{i}. {job}')
