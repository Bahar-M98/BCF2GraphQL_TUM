"""
locust_scaling.py — Automated scaling experiment: GraphQL vs REST at 1, 5, 10 users.

Defines BCFGraphQLUser and BCFRESTUser directly and runs all 6 experiments in
sequence headlessly, saving one CSV set per experiment under locust_results/.

Usage
─────
  # Run all 6 experiments automatically (recommended):
  python locust_scaling.py --host https://bcf2graphql.onrender.com

  # Override run time or pause:
  python locust_scaling.py --host https://bcf2graphql.onrender.com --run-time 3m --pause 15

  # Run a single experiment manually via Locust (class filter on CLI):
  locust -f locust_scaling.py BCFRESTUser    --headless --host ... --users 5 --spawn-rate 5 --run-time 5m --csv locust_results/rest_5users/stats
  locust -f locust_scaling.py BCFGraphQLUser --headless --host ... --users 5 --spawn-rate 5 --run-time 5m --csv locust_results/gql_5users/stats

Output layout
─────────────
  locust_results/
    rest_1user/   stats_stats.csv  stats_stats_history.csv  stats_failures.csv
    rest_5users/  …
    rest_10users/ …
    gql_1user/    …
    gql_5users/   …
    gql_10users/  …

Then view results:
  uv run streamlit run locust_scaling_dashboard.py
"""

import argparse
import os
import time as _time

from locust import HttpUser, between, task

# ── Configuration ──────────────────────────────────────────────────────────────

PROJECT_ID = os.environ.get("PROJECT_ID", "benchmark_small")

GQL_HEADERS  = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
REST_HEADERS = {"Accept-Encoding": "identity"}


# ── Chain-timing helper ────────────────────────────────────────────────────────

def _fire_chain(user: HttpUser, name: str, t0: float, total_bytes: int, exc=None):
    user.environment.events.request.fire(
        request_type="REST-chain",
        name=name,
        response_time=(_time.perf_counter() - t0) * 1000,
        response_length=total_bytes,
        exception=exc,
        context={},
    )


# ── GraphQL user ───────────────────────────────────────────────────────────────

class BCFGraphQLUser(HttpUser):
    wait_time = between(0.1, 0.5)

    def _gql(self, query: str, name: str):
        self.client.post(
            "/graphql/",
            json={"query": query, "variables": {"pid": PROJECT_ID}},
            headers=GQL_HEADERS,
            name=f"gql/{name}",
        )

    @task(1)
    def topics_flat(self):
        self._gql("""
            query($pid: ID!) {
              topics(projectId: $pid) {
                guid serverAssignedId topicType topicStatus
                referenceLinks title priority index labels
                creationDate { ISO8601 } creationAuthor
                modifiedDate { ISO8601 } modifiedAuthor
                dueDate       { ISO8601 }
                assignedTo stage description
                bimSnippet { snippetType isExternal reference referenceSchema }
                documentReferences { guid url documentGuid description }
                relatedTopics { guid }
              }
            }
        """, name="topics_flat")

    @task(3)
    def topics_nested(self):
        self._gql("""
            query($pid: ID!) {
              topics(projectId: $pid) {
                guid title topicStatus
                viewpoints {
                  guid
                  components { selection { ifcGuid originatingSystem } }
                }
              }
            }
        """, name="topics_nested")

    @task(1)
    def topic_events(self):
        self._gql("""
            query($pid: ID!) {
              topicEvents(projectId: $pid) {
                topicGuid date { ISO8601 } author
                actions { type value }
              }
            }
        """, name="topic_events")

    @task(1)
    def comment_events(self):
        self._gql("""
            query($pid: ID!) {
              commentEvents(projectId: $pid) {
                commentGuid topicGuid date { ISO8601 } author
                actions { type value }
              }
            }
        """, name="comment_events")

    @task(2)
    def project_comments(self):
        self._gql("""
            query($pid: ID!) {
              topics(projectId: $pid) {
                guid serverAssignedId topicType topicStatus
                referenceLinks title priority index labels
                creationDate  { ISO8601 } creationAuthor
                modifiedDate  { ISO8601 } modifiedAuthor
                dueDate       { ISO8601 }
                assignedTo stage description
                bimSnippet { snippetType isExternal reference referenceSchema }
                documentReferences { guid url documentGuid description }
                relatedTopics { guid }
                comments {
                  guid author comment
                  date { ISO8601 }
                  modifiedDate { ISO8601 } modifiedAuthor
                  viewpointGuid
                }
              }
            }
        """, name="project_comments")

    @task(2)
    def topic_full(self):
        self._gql("""
            query($pid: ID!) {
              topics(projectId: $pid) {
                guid title
                comments { guid author comment date { ISO8601 } }
                files    { fileName ifcProjectGuid date { ISO8601 } }
                viewpoints {
                  guid
                  components { selection { ifcGuid originatingSystem } }
                }
              }
            }
        """, name="topic_full")

    @task(1)
    def overfetch_partial(self):
        self._gql("""
            query($pid: ID!) {
              topics(projectId: $pid) { assignedTo creationAuthor }
            }
        """, name="overfetch_partial")


# ── REST user ──────────────────────────────────────────────────────────────────

class BCFRESTUser(HttpUser):
    wait_time = between(0.1, 0.5)

    def _get(self, path: str, name: str):
        r = self.client.get(path, headers=REST_HEADERS, name=f"rest/{name}")
        r.raise_for_status()
        return r.json(), len(r.content)

    @task(1)
    def topics_flat(self):
        self._get(f"/bcf/3.0/projects/{PROJECT_ID}/topics", name="topics_flat")

    @task(3)
    def topics_nested(self):
        t0    = _time.perf_counter()
        total = 0
        try:
            topics, b = self._get(f"/bcf/3.0/projects/{PROJECT_ID}/topics", name="topics_nested/topics")
            total += b
            for topic in topics:
                vps, vb = self._get(
                    f"/bcf/3.0/projects/{PROJECT_ID}/topics/{topic['guid']}/viewpoints",
                    name="topics_nested/viewpoints",
                )
                total += vb
                for vp in vps:
                    _, sb = self._get(
                        f"/bcf/3.0/projects/{PROJECT_ID}/topics/{topic['guid']}/viewpoints/{vp['guid']}/selection",
                        name="topics_nested/selection",
                    )
                    total += sb
        except Exception as exc:
            _fire_chain(self, "rest/topics_nested [chain]", t0, total, exc)
            return
        _fire_chain(self, "rest/topics_nested [chain]", t0, total)

    @task(1)
    def topic_events(self):
        self._get(f"/bcf/3.0/projects/{PROJECT_ID}/topics/events", name="topic_events")

    @task(1)
    def comment_events(self):
        self._get(f"/bcf/3.0/projects/{PROJECT_ID}/topics/comments/events", name="comment_events")

    @task(2)
    def project_comments(self):
        t0    = _time.perf_counter()
        total = 0
        try:
            topics, b = self._get(f"/bcf/3.0/projects/{PROJECT_ID}/topics", name="project_comments/topics")
            total += b
            for topic in topics:
                _, cb = self._get(
                    f"/bcf/3.0/projects/{PROJECT_ID}/topics/{topic['guid']}/comments",
                    name="project_comments/comments",
                )
                total += cb
        except Exception as exc:
            _fire_chain(self, "rest/project_comments [chain]", t0, total, exc)
            return
        _fire_chain(self, "rest/project_comments [chain]", t0, total)

    @task(2)
    def topic_full(self):
        t0    = _time.perf_counter()
        total = 0
        try:
            topics, b = self._get(f"/bcf/3.0/projects/{PROJECT_ID}/topics", name="topic_full/topics")
            total += b
            for topic in topics:
                guid = topic["guid"]
                _, cb = self._get(f"/bcf/3.0/projects/{PROJECT_ID}/topics/{guid}/comments",   name="topic_full/comments")
                _, fb = self._get(f"/bcf/3.0/projects/{PROJECT_ID}/topics/{guid}/files",      name="topic_full/files")
                vps, vb = self._get(f"/bcf/3.0/projects/{PROJECT_ID}/topics/{guid}/viewpoints", name="topic_full/viewpoints")
                total += cb + fb + vb
                for vp in vps:
                    _, sb = self._get(
                        f"/bcf/3.0/projects/{PROJECT_ID}/topics/{guid}/viewpoints/{vp['guid']}/selection",
                        name="topic_full/selection",
                    )
                    total += sb
        except Exception as exc:
            _fire_chain(self, "rest/topic_full [chain]", t0, total, exc)
            return
        _fire_chain(self, "rest/topic_full [chain]", t0, total)

    @task(1)
    def overfetch_partial(self):
        self._get(f"/bcf/3.0/projects/{PROJECT_ID}/topics", name="overfetch_partial")
import subprocess
import sys
import time
from pathlib import Path

# ── Experiment matrix ─────────────────────────────────────────────────────────

EXPERIMENTS = [
    {"label": "REST  —  1 user",   "cls": "BCFRESTUser",    "users": 1,  "out": "rest_1user"},
    {"label": "REST  —  5 users",  "cls": "BCFRESTUser",    "users": 5,  "out": "rest_5users"},
    {"label": "REST  — 10 users",  "cls": "BCFRESTUser",    "users": 10, "out": "rest_10users"},
    {"label": "GQL   —  1 user",   "cls": "BCFGraphQLUser", "users": 1,  "out": "gql_1user"},
    {"label": "GQL   —  5 users",  "cls": "BCFGraphQLUser", "users": 5,  "out": "gql_5users"},
    {"label": "GQL   — 10 users",  "cls": "BCFGraphQLUser", "users": 10, "out": "gql_10users"},
]

RESULTS_DIR      = Path(__file__).parent.parent / "locust_results"
DEFAULT_HOST     = "https://bcf2graphql.onrender.com"
DEFAULT_RUN_TIME = "5m"
DEFAULT_PAUSE    = 30   # seconds between runs


# ── Runner ────────────────────────────────────────────────────────────────────

def run_experiment(exp: dict, host: str, run_time: str) -> bool:
    out_dir = RESULTS_DIR / exp["out"]
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "locust",
        "-f", __file__,          # this file is a valid locustfile
        exp["cls"],              # filter to one user class
        "--headless",
        "--host",       host,
        "--users",      str(exp["users"]),
        "--spawn-rate", str(exp["users"]),   # spawn all users at once
        "--run-time",   run_time,
        "--csv",        str(out_dir / "stats"),
        "--csv-full-history",
    ]

    print(f"  cmd: {' '.join(cmd[2:])}")   # skip 'python -m' noise
    result = subprocess.run(cmd)
    return result.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run GraphQL vs REST scaling experiments and save CSVs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"Server URL (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--run-time", default=DEFAULT_RUN_TIME,
        help=f"Duration per experiment e.g. 5m, 3m (default: {DEFAULT_RUN_TIME})",
    )
    parser.add_argument(
        "--pause", type=int, default=DEFAULT_PAUSE,
        help=f"Seconds to pause between experiments (default: {DEFAULT_PAUSE})",
    )
    args = parser.parse_args()

    RESULTS_DIR.mkdir(exist_ok=True)
    n = len(EXPERIMENTS)

    print("BCF Locust Scaling Experiment")
    print(f"Host     : {args.host}")
    print(f"Run time : {args.run_time} per experiment")
    print(f"Pause    : {args.pause}s between runs")
    print(f"Results  : {RESULTS_DIR}/")
    print("=" * 60)

    failed = []
    for i, exp in enumerate(EXPERIMENTS, 1):
        print(f"\n[{i}/{n}] {exp['label']}")
        ok = run_experiment(exp, args.host, args.run_time)
        if ok:
            print(f"  ✓ saved → {RESULTS_DIR / exp['out']}/")
        else:
            print(f"  ✗ experiment failed — continuing")
            failed.append(exp["label"])

        if i < n:
            print(f"  ⏸  pausing {args.pause}s …")
            time.sleep(args.pause)

    print(f"\n{'=' * 60}")
    print(f"Done. {n - len(failed)}/{n} experiments succeeded.")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"\nView results:")
    print(f"  uv run streamlit run locust_scaling_dashboard.py")


if __name__ == "__main__":
    main()
