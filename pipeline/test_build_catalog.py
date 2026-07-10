from __future__ import annotations

import unittest

from pipeline.build_catalog import build_catalog


def repository(repo: str, stars: int, created_at: str, description: str = "AI developer tool"):
    return {
        "repo": repo,
        "url": f"https://github.com/{repo}",
        "description": description,
        "language": "Python",
        "license": "MIT",
        "topics": ["developer-tools", "ai"],
        "stars": stars,
        "forks": 20,
        "created_at": created_at,
        "pushed_at": "2026-07-10T09:00:00Z",
        "candidate_query": "one | two",
    }


class BuildCatalogTests(unittest.TestCase):
    def test_static_analysis_can_raise_reuse_confidence(self) -> None:
        snapshot = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
        }
        analysis = {
            "repository": "demo/new-tool",
            "scanned_files": 240,
            "confidence": 91,
            "indicators": {
                "readme": True,
                "license": True,
                "tests": True,
                "ci": True,
                "docs": True,
                "examples": True,
                "package_manifest": True,
                "dependency_lock": True,
            },
            "counts": {"test_files": 18, "todo_markers": 2},
        }

        facts_only = build_catalog(snapshot)
        inspected = build_catalog(snapshot, analyses={"demo/new-tool": analysis})

        self.assertLessEqual(facts_only["projects"][0]["reuseScore"], 72)
        self.assertGreater(inspected["projects"][0]["reuseScore"], facts_only["projects"][0]["reuseScore"])
        self.assertEqual(inspected["projects"][0]["analysisState"], "静态分析")
        self.assertEqual(len(inspected["projects"][0]["evidence"]), 3)

    def test_codex_enrichment_replaces_copy_and_adds_task_terms(self) -> None:
        snapshot = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
        }
        enrichment = {
            "repository": "demo/new-tool",
            "titleZh": "自动化工作流工具",
            "summaryZh": "把重复开发步骤组织成可复用流程。",
            "category": "开发工具",
            "capabilities": ["流程编排", "代码生成"],
            "taskTerms": ["工作流", "脚本生成"],
            "bestFor": "适合需要减少重复编码的开发任务。",
            "reusePlan": "优先复用流程定义层。",
            "limitation": "尚未运行验证。",
            "sourceUrl": "https://github.com/demo/new-tool",
        }

        catalog = build_catalog(snapshot, enrichments={"demo/new-tool": enrichment})
        project = catalog["projects"][0]

        self.assertEqual(project["title"], "自动化工作流工具")
        self.assertEqual(project["analysisState"], "深度分析")
        self.assertIn("脚本生成", project["taskTerms"])
        self.assertEqual(project["reusePlan"], "优先复用流程定义层。")
        self.assertEqual(len(project["evidence"]), 3)
        self.assertEqual(catalog["deepAnalysisCount"], 1)
        self.assertEqual(catalog["pendingDeepAnalysis"], [])

    def test_first_snapshot_labels_velocity_as_proxy(self) -> None:
        snapshot = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
        }

        catalog = build_catalog(snapshot)

        project = catalog["projects"][0]
        self.assertEqual(project["growthKind"], "velocity_proxy")
        self.assertIn("首次观察代理", project["growthLabel"])
        self.assertNotIn("24 小时新增", project["whyNow"])
        self.assertEqual(catalog["growthMode"], "first_observation_proxy")

    def test_second_snapshot_reports_observed_window(self) -> None:
        previous = {
            "captured_at": "2026-07-09T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 700, "2026-07-07T12:00:00Z")],
        }
        current = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 1,
            "repositories": [repository("demo/new-tool", 900, "2026-07-07T12:00:00Z")],
        }

        catalog = build_catalog(current, previous)

        project = catalog["projects"][0]
        self.assertEqual(project["growthKind"], "observed")
        self.assertEqual(project["growthValue"], 200)
        self.assertIn("24.0 小时", project["growthLabel"])

    def test_recent_actionable_repository_outranks_old_reference_list(self) -> None:
        snapshot = {
            "captured_at": "2026-07-10T12:00:00Z",
            "count": 2,
            "repositories": [
                repository("demo/agent-workflow", 900, "2026-07-07T12:00:00Z"),
                repository("demo/awesome-books", 200_000, "2015-01-01T00:00:00Z", "Awesome books roadmap"),
            ],
        }

        catalog = build_catalog(snapshot)

        self.assertEqual(catalog["projects"][0]["repo"], "demo/agent-workflow")


if __name__ == "__main__":
    unittest.main()
