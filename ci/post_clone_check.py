#!/usr/bin/env python3
"""Publish the report as a single merge request note."""

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


MARKER = "<!-- clone-check-poc -->"


def api(root, token, path, method="GET", body=None):
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    request = Request(f"{root}{path}", headers=headers, method=method, data=data)
    with urlopen(request, timeout=20) as response:
        return json.load(response), response.headers.get("X-Next-Page")


def sync_note(root, token, project, iid, branch, target, head, report):
    mr_path = f"/projects/{project}/merge_requests/{iid}"
    merge_request, _ = api(root, token, mr_path)
    if (
        merge_request["state"] != "opened" or merge_request["sha"] != head
        or merge_request["target_branch"] != branch
    ):
        print("Skipped comment for an outdated or closed merge request.")
        return

    branch_path = f"/projects/{project}/repository/branches/{quote(branch, safe='')}"
    target_branch, _ = api(root, token, branch_path)
    if target_branch["commit"]["id"] != target:
        print("Skipped comment because the target branch advanced. Run the MR pipeline again.")
        return

    user, _ = api(root, token, "/user")
    notes_path = f"{mr_path}/notes"
    page = "1"
    previous = None
    while page and previous is None:
        notes, page = api(root, token, f"{notes_path}?per_page=100&page={page}")
        previous = next(
            (note for note in notes if note["author"]["id"] == user["id"]
             and note["body"].startswith(f"{MARKER}\n")),
            None,
        )

    if not report and previous is None:
        return
    body = f"{MARKER}\n{report or 'クローン先への変更反映に関する指摘はありません。'}"
    if previous is None:
        api(root, token, notes_path, "POST", {"body": body})
    elif previous["body"] != body:
        api(root, token, f"{notes_path}/{previous['id']}", "PUT", {"body": body})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    token = os.environ.get("MR_CLONE_COMMENT_TOKEN")
    if not token:
        print("MR_CLONE_COMMENT_TOKEN is unset. Check output is available in the job log.")
        return 0
    try:
        sync_note(
            os.environ["CI_API_V4_URL"].rstrip("/"), token,
            int(os.environ["CI_MERGE_REQUEST_PROJECT_ID"]),
            int(os.environ["CI_MERGE_REQUEST_IID"]),
            os.environ["CI_MERGE_REQUEST_TARGET_BRANCH_NAME"], args.target, args.head,
            args.report.read_text(encoding="utf-8").strip(),
        )
    except (URLError, OSError, ValueError, KeyError) as error:
        print(f"GitLab comment failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
