"""Corporate notification sinks: GitLab, Bitbucket, Azure DevOps (PR comments), PagerDuty, Opsgenie, Jira and Teams."""
from __future__ import annotations

import base64
import urllib.parse

from intentflow.notify import env, post_json

SINK_TYPES = ("gitlab", "bitbucket", "azure_devops", "pagerduty", "opsgenie", "jira", "teams")
PD_SEVERITY = {"critical": "critical", "high": "error", "medium": "warning", "low": "info", "info": "info"}
OG_PRIORITY = {"critical": "P1", "high": "P2", "medium": "P3", "low": "P4", "info": "P5"}


def _basic(user: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


def deliver(sink: dict, subject: str, body: str, meta: dict):
    """True if this module handled the sink type; None otherwise."""
    t = sink["type"]
    sev = meta.get("severity", "medium")
    if t == "gitlab":          # merge request note
        api = sink.get("api", "https://gitlab.com/api/v4").rstrip("/")
        proj = urllib.parse.quote(str(sink["project"]), safe="")
        post_json(f"{api}/projects/{proj}/merge_requests/{sink['mr']}/notes", {"body": body}, {"PRIVATE-TOKEN": env(sink, "token_env", "GITLAB_TOKEN")})
    elif t == "bitbucket":     # Bitbucket Cloud: pull request comment
        api = sink.get("api", "https://api.bitbucket.org/2.0").rstrip("/")
        post_json(f"{api}/repositories/{sink['workspace']}/{sink['repo']}/pullrequests/{sink['pr']}/comments", {"content": {"raw": body}},
                  {"Authorization": f"Bearer {env(sink, 'token_env', 'BITBUCKET_TOKEN')}"})
    elif t == "azure_devops":  # pull request comment thread (PAT)
        api = sink.get("api", "https://dev.azure.com").rstrip("/")
        url = f"{api}/{sink['org']}/{sink['project']}/_apis/git/repositories/{sink['repo']}/pullRequests/{sink['pr']}/threads?api-version=7.1"
        post_json(url, {"comments": [{"parentCommentId": 0, "content": body, "commentType": 1}], "status": 1},
                  {"Authorization": _basic("", env(sink, "token_env", "AZURE_DEVOPS_PAT"))})
    elif t == "pagerduty":     # Events API v2; dedup_key = incident id (the same incident never opens two alerts)
        url = sink.get("url", "https://events.pagerduty.com/v2/enqueue")
        post_json(url, {"routing_key": env(sink, "routing_key_env", "PAGERDUTY_ROUTING_KEY"), "event_action": "trigger",
                        "dedup_key": meta.get("id") or subject,
                        "payload": {"summary": subject[:1000], "source": sink.get("source", "intentflow"), "severity": PD_SEVERITY.get(sev, "warning"),
                                    "custom_details": {"details": body[:5000]}}})
    elif t == "opsgenie":
        api = sink.get("api", "https://api.opsgenie.com").rstrip("/")
        post_json(f"{api}/v2/alerts", {"message": subject[:130], "alias": meta.get("id") or subject[:512], "description": body[:15000],
                                       "priority": OG_PRIORITY.get(sev, "P3"), "source": "intentflow"},
                  {"Authorization": f"GenieKey {env(sink, 'token_env', 'OPSGENIE_API_KEY')}"})
    elif t == "jira":          # REST v2 accepts a plain-text description (Cloud and Server); one issue per delivery
        base = sink["url"].rstrip("/")
        post_json(f"{base}/rest/api/2/issue", {"fields": {"project": {"key": sink["project"]}, "summary": subject[:250], "description": body[:30000],
                                                          "issuetype": {"name": sink.get("issue_type", "Task")},
                                                          **({"labels": sink["labels"]} if sink.get("labels") else {})}},
                  {"Authorization": _basic(env(sink, "user_env", "JIRA_USER"), env(sink, "token_env", "JIRA_TOKEN"))})
    elif t == "teams":         # Teams Workflows ("Post to a channel when a webhook request is received"): Adaptive Card
        card = {"type": "AdaptiveCard", "version": "1.4", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "body": [{"type": "TextBlock", "text": subject, "weight": "Bolder", "wrap": True},
                         {"type": "TextBlock", "text": body[:3000], "wrap": True}]}
        post_json(env(sink, "url_env", "TEAMS_URL"), {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]})
    else:
        return None
    return True
