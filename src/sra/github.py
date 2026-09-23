"""Small GitHub REST adapter. Authentication stays in the host process."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import GitHubError, SRAError
from .models import Tracker, WorkSpec, digest
from .specs import MARKER, issue_body, parse_issue


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GitHubError(code, "Redirect refused; verify the configured repository identity")


class GitHubTracker:
    def __init__(self, config: Tracker):
        self.config = config
        self.repository = config.repository
        self.root = "https://api.github.com/repos/" + self.repository
        self.opener = urllib.request.build_opener(NoRedirect())

    @property
    def token(self) -> str:
        token = os.environ.get(self.config.token_env)
        if not token and self.config.token_env == "GITHUB_TOKEN":
            token = os.environ.get("GH_TOKEN")
        if not token:
            raise SRAError(f"Set {self.config.token_env} on the host; never put credentials in WORKFLOW.md")
        return token

    def request(self, method: str, path: str, data=None):
        if not path.startswith("/") or path.startswith("//") or ".." in path.split("?")[0]:
            raise SRAError("Invalid repository API path")
        body = json.dumps(data).encode() if data is not None else None
        headers = {"Accept": "application/vnd.github+json", "Authorization": "Bearer " + self.token,
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "sra/0.1.0"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.root + path, data=body, headers=headers, method=method)
        # GET is safe to retry. Ambiguous writes are reconciled by their callers, never blindly replayed.
        for attempt in range(3 if method == "GET" else 1):
            try:
                with self.opener.open(request, timeout=30) as response:
                    payload = response.read(8_388_609)
                    if len(payload) > 8_388_608:
                        raise SRAError("GitHub response exceeds 8 MiB")
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as exc:
                if method == "GET" and exc.code in (429, 502, 503, 504) and attempt < 2:
                    exc.close()
                    time.sleep(2 ** attempt)
                    continue
                detail = exc.read(1000).decode(errors="replace")
                exc.close()
                raise GitHubError(exc.code, detail) from exc
            except urllib.error.URLError as exc:
                if method == "GET" and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise SRAError(f"GitHub request failed: {exc.reason}; writes may need reconciliation") from exc
        raise SRAError("GitHub request exhausted retry budget")

    def pages(self, path: str) -> list[dict]:
        result = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 101):
            batch = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise SRAError("Unexpected GitHub collection response")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise SRAError("GitHub pagination exceeds 10,000 objects; narrow the tracker scope")

    def issue(self, number: int) -> dict:
        if number <= 0:
            raise SRAError("Issue number must be positive")
        value = self.request("GET", f"/issues/{number}")
        if "pull_request" in value:
            raise SRAError("Dispatch identity must be an Issue, not a PR; attach the PR to the original Issue")
        return value

    def eligible(self, issue: dict) -> bool:
        labels = {x["name"].casefold() for x in issue.get("labels", [])}
        author = issue.get("user", {}).get("login", "").casefold()
        allowed = {x.casefold() for x in self.config.allowed_authors}
        return ("pull_request" not in issue and issue.get("state") == "open"
                and self.config.ready_label.casefold() in labels and (not allowed or author in allowed))

    def candidates(self) -> list[dict]:
        query = urllib.parse.urlencode({"state": "open", "labels": self.config.ready_label})
        return [x for x in self.pages("/issues?" + query) if self.eligible(x)]

    def ensure_label(self) -> None:
        label = urllib.parse.quote(self.config.ready_label, safe="")
        try:
            self.request("GET", "/labels/" + label)
        except GitHubError as exc:
            if exc.status != 404:
                raise
            self.request("POST", "/labels", {"name": self.config.ready_label, "color": "496b8a",
                                             "description": "Eligible after local SRA approval"})

    def publish(self, spec: WorkSpec, *, ready: bool) -> dict:
        matches = []
        for item in self.pages("/issues?state=all"):
            if "pull_request" in item or MARKER not in (item.get("body") or ""):
                continue
            try:
                existing = parse_issue(item["body"])
            except (ValueError, SRAError):
                continue
            if existing.id == spec.id:
                matches.append((item, existing))
        if len(matches) > 1:
            raise SRAError("Multiple Issues have this WorkSpec id; resolve ambiguity before publishing")
        if ready:
            self.ensure_label()
        if matches:
            item, existing = matches[0]
            if item["state"] != "open":
                raise SRAError("This WorkSpec already has a closed Issue; use a new id for new work")
            if digest(existing) != digest(spec):
                if spec.revision <= existing.revision:
                    raise SRAError("Increment spec revision before updating a published contract")
                item = self.request("PATCH", f"/issues/{item['number']}",
                                    {"title": spec.title, "body": issue_body(spec)})
            if ready:
                self.request("POST", f"/issues/{item['number']}/labels", {"labels": [self.config.ready_label]})
            return item
        return self.request("POST", "/issues", {"title": spec.title, "body": issue_body(spec),
                                                 "labels": [self.config.ready_label] if ready else []})

    def workpad(self, number: int, text: str) -> None:
        marker = "<!-- sra:workpad:v1 -->"
        comments = self.pages(f"/issues/{number}/comments")
        existing = [x for x in comments if (x.get("body") or "").startswith(marker)]
        if len(existing) > 1:
            raise SRAError("Multiple SRA workpads found; refusing ambiguous updates")
        payload = {"body": marker + "\n## Research Agent Workpad\n\n" + text}
        if existing:
            self.request("PATCH", f"/issues/comments/{existing[0]['id']}", payload)
        else:
            self.request("POST", f"/issues/{number}/comments", payload)

    def pull_request(self, branch: str, base: str, title: str, body: str) -> dict:
        owner = self.repository.split("/")[0]
        query = urllib.parse.urlencode({"head": owner + ":" + branch, "state": "all"})
        found = self.pages("/pulls?" + query)
        if found:
            if len(found) != 1 or found[0]["state"] != "open" or found[0]["base"]["ref"] != base:
                raise SRAError("Branch has a closed, merged, or mismatched PR; refusing to repurpose it")
            # Do not overwrite human edits to an existing PR description.
            return found[0]
        return self.request("POST", "/pulls", {"title": title, "head": branch, "base": base,
                                               "body": body, "draft": True, "maintainer_can_modify": True})
