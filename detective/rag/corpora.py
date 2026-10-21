"""The four corpora (report §6.1) and the two retrieval points that use them.

  code     AST function/class chunks at a given commit (SHA stored as metadata)
  docs     README / docs / ADRs / notes, chunked per heading
  history  closed issues and merged PR descriptions, one document per thread
  ci       past failure events with their eventual resolution (resolution + fix commit as metadata)
"""
from __future__ import annotations

import json
import re

from ..tools.ast_tools import chunk_python, defs_overlapping
from ..tools.forge import Forge
from ..tools.git import GitRepo, parse_hunks
from .index import Doc, HashingEmbedder, HybridIndex

DOC_EXT = (".md", ".rst", ".txt", ".adoc")
MAX_PY_FILES = 600
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$", re.M)


def code_docs(repo: GitRepo, sha: str, only: list[str] | None = None) -> list[Doc]:
    paths = only if only is not None else [p for p in repo.ls_files(sha) if p.endswith(".py")][:MAX_PY_FILES]
    out = []
    for p in paths:
        if not p.endswith(".py"):
            continue
        src = repo.show_file(sha, p)
        if src is None:
            continue
        for c in chunk_python(src, p):
            out.append(Doc(id=f"code:{sha[:7]}:{p}:{c.qualname}", corpus="code", title=f"{p} :: {c.qualname}",
                           text=c.text[:5000],
                           meta={"sha": sha, "path": p, "qualname": c.qualname, "kind": c.kind,
                                 "start": c.start, "end": c.end, "is_test": _is_test(p),
                                 "identifiers": c.identifiers[:80]}))
    return out


def doc_docs(repo: GitRepo, sha: str) -> list[Doc]:
    out = []
    for p in repo.ls_files(sha):
        if not p.lower().endswith(DOC_EXT) or p.lower().startswith(("license", "requirements")):
            continue
        txt = repo.show_file(sha, p) or ""
        for heading, body in _split_headings(txt):
            if body.strip():
                out.append(Doc(id=f"docs:{sha[:7]}:{p}#{heading}", corpus="docs", title=f"{p} — {heading}",
                               text=body[:5000], meta={"sha": sha, "path": p, "heading": heading}))
    return out


def history_docs(forge: Forge) -> list[Doc]:
    out = []
    for it in forge.history_items():
        kind = it.get("kind", "issue")
        num = it.get("number")
        out.append(Doc(id=f"history:{kind}:{num}", corpus="history", title=f"{kind.upper()} #{num}: {it.get('title', '')}",
                       text=(it.get("body") or "")[:6000],
                       meta={"kind": kind, "number": num, "files": it.get("files", []),
                             "closed_at": it.get("closed_at"), "url": it.get("url"),
                             "labels": it.get("labels", []), "commit": it.get("commit")}))
    return out


def ci_docs(forge: Forge) -> list[Doc]:
    out = []
    for ev in forge.ci_archive():
        text = "\n".join(filter(None, [ev.get("test_id"), ev.get("message"), (ev.get("log") or "")[:3000],
                                       ev.get("notes")]))
        out.append(Doc(id=f"ci:{ev.get('id')}", corpus="ci", title=f"CI failure {ev.get('id')}: {ev.get('test_id', '')}",
                       text=text, meta={"resolution": ev.get("resolution", "unknown"),
                                        "fix_commit": ev.get("fix_commit"), "test_id": ev.get("test_id", ""),
                                        "date": ev.get("date")}))
    return out


def _is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{path}" or name == "conftest.py"


def _split_headings(txt: str) -> list[tuple[str, str]]:
    marks = list(_HEADING.finditer(txt))
    if not marks:
        return [("(top)", txt)]
    out = []
    if marks[0].start() > 0:
        out.append(("(top)", txt[: marks[0].start()]))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(txt)
        out.append((m.group(2).strip(), txt[m.start(): end]))
    return out


class Retriever:
    def __init__(self, repo: GitRepo, forge: Forge, embedder=None, use_history: bool = True):
        self.repo = repo
        self.forge = forge
        self.embedder = embedder or HashingEmbedder()
        self.use_history = use_history
        self.index = HybridIndex(self.embedder)
        self.indexed_shas: set[str] = set()
        self.stats: dict[str, int] = {}

    def build(self, sha: str) -> dict[str, int]:
        docs = code_docs(self.repo, sha) + doc_docs(self.repo, sha) + history_docs(self.forge)
        if self.use_history:
            docs += ci_docs(self.forge)
        self.index.add(docs)
        self.index.build()
        self.indexed_shas.add(sha)
        self.stats = {c: sum(d.corpus == c for d in self.index.docs) for c in ("code", "docs", "history", "ci")}
        self.stats["embedder"] = self.index.embedder.name + (f" — {self.index.note}" if self.index.note else "")  # type: ignore[assignment]
        return self.stats

    def add_commit(self, sha: str, paths: list[str]) -> None:
        """Incremental: index only the files a commit touched, tagged with that commit's SHA."""
        if sha in self.indexed_shas:
            return
        self.index.add(code_docs(self.repo, sha, only=[p for p in paths if p.endswith(".py")]))
        self.index.build()
        self.indexed_shas.add(sha)

    # ----------------------------------------------------------------- retrieval point one
    def similar_failures(self, symptom: str, k: int = 5) -> list[tuple[Doc, float, dict]]:
        if not self.use_history:
            return []
        return self.index.search(symptom, k=k, where=lambda d: d.corpus == "ci")

    # ----------------------------------------------------------------- retrieval point two
    def context_for_commit(self, culprit: str, failure: str, test_path: str, test_name: str,
                           k: int = 4) -> dict:
        diff = self.repo.commit_diff(culprit)
        changed = [p for _, p in self.repo.changed_files(culprit)]
        self.add_commit(culprit, changed)
        parent = self.repo.commit(culprit).parents[0] if self.repo.commit(culprit).parents else None
        hunks = parse_hunks(diff)
        touched = []
        for path, h in hunks.items():
            if not path.endswith(".py"):
                continue
            new_src = self.repo.show_file(culprit, path) or ""
            old_src = (self.repo.show_file(parent, h.get("old_path") or path) or "") if parent else ""
            for c in defs_overlapping(new_src, path, h["new"]):
                old = next((o for o in defs_overlapping(old_src, path, h["old"]) if o.qualname == c.qualname), None)
                touched.append({"path": path, "qualname": c.qualname, "after": c.text[:4000],
                                "before": old.text[:4000] if old else None})
            for o in defs_overlapping(old_src, path, h["old"]):
                if not any(t["qualname"] == o.qualname and t["path"] == path for t in touched):
                    touched.append({"path": path, "qualname": o.qualname, "after": None, "before": o.text[:4000]})
        names = sorted({t["qualname"].split(".")[-1] for t in touched})
        ident_query = " ".join(names) + " " + self.repo.commit(culprit).subject + " " + failure

        covering = self.index.search(" ".join(names) + " " + test_name, k=k,
                                     where=lambda d: d.corpus == "code" and d.meta.get("is_test")
                                     and any(n in d.text for n in names) if names else False)
        docs = self.index.search(ident_query, k=k, where=lambda d: d.corpus == "docs")
        history = self.index.search(ident_query, k=k, where=lambda d: d.corpus == "history")
        by_files = [d for d in self.index.docs if d.corpus == "history"
                    and set(d.meta.get("files") or []) & set(changed)]
        prior = []
        for c in self.repo.log_for_paths([p for p in changed if p.endswith(".py")], f"{culprit}^", n=5):
            prior.append({"sha": c.sha, "short": c.short, "subject": c.subject, "date": c.date})
        return {"diff": diff, "changed": changed, "touched": touched, "covering_tests": covering,
                "docs": docs, "history": history, "history_by_files": by_files, "prior_commits": prior}


def make_embedder(settings, llm_client) -> object:
    if llm_client is not None and settings.llm_enabled:
        from .index import GeminiEmbedder

        return GeminiEmbedder(llm_client, settings.gemini_embed_model, settings.cache_dir)
    return HashingEmbedder()


def dump_hits(hits) -> str:
    return "\n\n".join(f"[{d.id}] score={s:.4f} {json.dumps(r)}\n{d.title}\n{d.text[:1500]}" for d, s, r in hits)
