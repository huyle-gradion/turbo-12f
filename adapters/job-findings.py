#!/usr/bin/env python3
import json
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def _response_body(response):
    return response[1] if isinstance(response, tuple) else response


def is_open(fetch, repo, number, token):
    if number is None:
        return False
    response = fetch("GET", f"https://api.github.com/repos/{repo}/issues/{number}", token, None)
    if isinstance(response, tuple) and response[0] in (404, 410):
        return False
    return (_response_body(response) or {}).get("state") != "closed"


def _detail(detail):
    if "<" in detail:
        return common.untrusted_block(detail).rstrip("\n")
    detail = re.sub(r'(github\.com/[^\s/]+/[^\s/]+/(?:issues|pull)/)(\d+)', r'\1 \2', detail, flags=re.IGNORECASE)
    return re.sub(r'(#|GH-)(\d+)', r'\1 \2', detail, flags=re.IGNORECASE)


def _entry(finding):
    title = finding["title"]
    bullet = "" if "<" in title else "- "
    shown = _detail(title)
    return f"{bullet}{shown}\n{_detail(finding.get('detail', ''))}"


def main(argv=None, fetch=None):
    if any(arg == "--label" or arg.startswith("--label=") for arg in argv):
        print("job-findings: --label is not supported", file=sys.stderr)
        return 2
    fetch = fetch or common.request
    findings_path = argv[argv.index("--findings") + 1]
    with open(findings_path) as handle:
        record = json.load(handle)
    findings = record["findings"]
    repo = argv[argv.index("--repo") + 1]
    name = argv[argv.index("--name") + 1] if "--name" in argv else record.get("job")
    if "--home" in argv and name:
        job_dir = os.path.join(argv[argv.index("--home") + 1], "jobs", name)
    else:
        job_dir = os.path.dirname(os.path.abspath(findings_path))
    store_path = os.path.join(job_dir, "fingerprints.json")
    store = {}
    if os.path.exists(store_path):
        with open(store_path) as handle:
            store = json.load(handle)
    token = os.environ.get("GITHUB_TOKEN")
    findings = [finding for finding in findings
                if not is_open(fetch, repo, store.get(finding["fingerprint"]), token)]
    if not findings:
        return 0
    payload = {
        "title": f"{len(findings)} findings",
        "body": "\n".join(_entry(finding) for finding in findings),
    }
    response = fetch("POST", f"https://api.github.com/repos/{repo}/issues",
                     token, payload)
    if isinstance(response, tuple) and (response[0] is None or response[0] >= 400):
        reason = response[2] if response[0] is None else f"status {response[0]}"
        print(f"job-findings: create failed with {reason}", file=sys.stderr)
        return 1
    number = (_response_body(response) or {}).get("number")
    store.update({finding["fingerprint"]: number for finding in findings})
    os.makedirs(job_dir, exist_ok=True)
    with open(store_path, "w") as handle:
        json.dump(store, handle)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
