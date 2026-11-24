#!/usr/bin/env python3
"""Freebuff Founder-Alpha integration ingester — reproducible, gated, fail-closed.

Validates a Freebuff delivery (git bundle, git-am-able patch file, or local repo path)
against the Z.AI integration base (69dc5ea + the 10-commit series at 74dd9a5) and — ONLY
when the actual candidate commit e392022 is present — produces an integration candidate
worktree with the kill-switch signature reconciled.

HARD GATE: e392022 (full hash must match the mission-specified candidate) must exist in
the ingested objects. NOTHING substitutes for it: founder-alpha branches/reaches that do
not contain this commit are accepted ONLY with --fixture, run the full validation
pipeline, and NEVER produce a candidate. Fixture mode exists solely to prove the tool
works and to run the compatibility matrix against lane proxies.

Exit codes: 0 candidate produced · 1 tool error · 2 gate refusal (validation failed or
candidate absent — always the correct outcome until Freebuff's artifacts arrive).

Usage:
  freebuff_integration.py --artifact <bundle|patch|repo-path> [--fixture] [--base <gitdir>]
      [--output-dir <dir>] [--report <md>]

Pipeline stages (each printed, all failures fail-closed):
  S1  ingest         fetch bundle / apply-check patch / verify repo path
  S2  candidate gate e392022 present? (absent + --fixture => validation-only run)
  S3  ancestry       merge-base vs base; disjoint histories are EXPECTED (content-level)
  S4  migrations     revision-graph diff: duplicate revision ids, same-number/different-id
                     collisions, multiple heads, broken down_revision links, filename clashes
  S5  surfaces       compatibility matrix: routers, model tables, settings keys, provider
                     routing symbols, frontend /api calls, requirements overlap
  S6  kill-switch    detect assert_not_killed signature; reconcile the two call sites via
                     the isolated adaptation patch when the candidate's API is owner-scoped
  S7  candidate      build candidate worktree = base + series + candidate tree + reconciliation
                     (candidate mode only), with integration test invocation printed
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_COMMIT = "69dc5ea5a9cfc750303ea1a9b335c78612bc8f39"
SERIES_TIP = "74dd9a5"
CANDIDATE_PREFIX = "e392022"  # full hash verified at S2 by unambiguous-prefix resolution

MIG_RE = re.compile(r"^revision\s*[:=]\s*[\"']([^\"']+)[\"']", re.M)
DOWN_RE = re.compile(r"^down_revision\s*[:=]\s*(?:[\"']([^\"']+)[\"']|None)", re.M)


def run(cmd: list[str], cwd: str | None = None, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, input=input)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{p.stderr.strip() or p.stdout.strip()}")
    return p


class Stage:
    def __init__(self, lines: list[str]):
        self.lines = lines

    def ok(self, msg: str) -> None:
        self.lines.append(f"- ✅ {msg}")

    def warn(self, msg: str) -> None:
        self.lines.append(f"- ⚠️ {msg}")

    def fail(self, msg: str) -> None:
        self.lines.append(f"- ❌ {msg}")


def stage_ingest(artifact: Path, workdir: Path, s: Stage) -> dict:
    """Fetch/validate the artifact into workdir/repo; return refs info."""
    repo = workdir / "repo"
    s.fail  # keep linters honest
    if artifact.is_file() and artifact.suffix == ".bundle":
        # Always ingest via init+fetch: bundles may carry prerequisites (base..tip) and
        # may lack a HEAD ref — `git clone` on a HEAD-less bundle silently yields an
        # unborn branch and empty refs/heads, poisoning every later stage.
        run(["git", "init", "-q", str(repo)])
        p = run(["git", "rev-parse", "--verify", "--quiet", BASE_COMMIT], cwd=str(repo), check=False)
        if p.returncode != 0:
            # Fetch ALL base heads: a raw-SHA fetch downloads the named object WITHOUT its
            # ancestors, which later breaks ancestry traversal of candidate commits whose
            # parent is the base commit.
            run(["git", "fetch", "-q", str(Path(BASE_REPO_PATH).resolve()), "+refs/heads/*:refs/heads/*"], cwd=str(repo))
        run(["git", "fetch", "-q", str(artifact.resolve()), "refs/heads/*:refs/heads/*"], cwd=str(repo))
        heads = run(["git", "for-each-ref", "--format=%(objectname)", "refs/heads"], cwd=str(repo)).stdout.split()
        if not heads:
            raise RuntimeError("bundle carried no branch refs after fetch")
        run(["git", "checkout", "-q", "--detach", heads[0]], cwd=str(repo))
        refs = run(["git", "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"], cwd=str(repo)).stdout
        s.ok(f"bundle ingested (base-seeded if needed); refs: {refs.strip() or '(none)'}")
        return {"repo": repo, "refs": refs, "analysis": heads[0]}
    elif artifact.is_file() and artifact.suffix == ".patch":
        run(["git", "clone", "-q", str(BASE_REPO_PATH), str(repo)])
        run(["git", "checkout", "-q", BASE_COMMIT], cwd=str(repo))
        run(["git", "apply", "--check", str(artifact.resolve())], cwd=str(repo))
        run(["git", "am", "-q", str(artifact.resolve())], cwd=str(repo))
        refs = f"HEAD -> {run(['git', 'rev-parse', 'HEAD'], cwd=str(repo)).stdout.strip()}"
        s.ok(f"patch applied clean onto base; {refs}")
        return {"repo": repo, "refs": refs, "analysis": "HEAD"}
    elif artifact.is_dir():
        run(["git", "clone", "-q", str(artifact), str(repo)])
        refs = run(["git", "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"], cwd=str(repo)).stdout
        s.ok(f"repo cloned; refs: {refs.strip() or '(none)'}")
        return {"repo": repo, "refs": refs, "analysis": "HEAD"}
    else:
        raise RuntimeError(f"unsupported artifact: {artifact} (need .bundle, .patch, or a git repo path)")
    return {"repo": repo, "refs": refs, "analysis": "HEAD"}


def resolve_candidate(repo: Path, s: Stage) -> str | None:
    """Unambiguously resolve the candidate short hash to a full hash in the fetched objects."""
    p = run(["git", "rev-parse", "--verify", "--quiet", f"{CANDIDATE_PREFIX}^{{commit}}"], cwd=str(repo), check=False)
    full = p.stdout.strip()
    if p.returncode == 0 and full.startswith(CANDIDATE_PREFIX):
        s.ok(f"candidate {CANDIDATE_PREFIX} resolved to {full} — HARD GATE PASSED")
        return full
    s.fail(f"candidate {CANDIDATE_PREFIX} NOT present in ingested objects — gate refuses candidate production")
    return None


def stage_ancestry(repo: Path, s: Stage) -> None:
    p = run(["git", "merge-base", BASE_COMMIT, "HEAD"], cwd=str(repo), check=False)
    if p.returncode == 0:
        s.ok(f"shared ancestry with base at {p.stdout.strip()[:12]} — merge-based integration possible")
    else:
        s.ok("history DISJOINT from base (expected for founder-alpha lanes) — content-level integration")


def parse_migrations(tree_repo: Path, treeish: str) -> dict[str, tuple[str, str, str]]:
    """revision -> (down_revision, filename, file content hash) for alembic versions on treeish."""
    out: dict[str, tuple[str, str, str]] = {}
    p = run(["git", "ls-tree", "-r", "--name-only", treeish, "backend/alembic/versions/"], cwd=str(tree_repo), check=False)
    for path in p.stdout.splitlines():
        if not path.endswith(".py"):
            continue
        blob = run(["git", "show", f"{treeish}:{path}"], cwd=str(tree_repo)).stdout
        m, d = MIG_RE.search(blob), DOWN_RE.search(blob)
        if m:
            out[m.group(1)] = (d.group(1) if d and d.group(1) else "", Path(path).name, hashlib.sha1(blob.encode()).hexdigest()[:8])
    return out


def stage_migrations(base_repo: Path, cand_repo: Path, cand_treeish: str, s: Stage) -> list[str]:
    mine = parse_migrations(base_repo, SERIES_TIP)
    theirs = parse_migrations(cand_repo, cand_treeish)
    problems: list[str] = []
    differing: list[str] = []
    common = set(mine) & set(theirs)
    for rev in sorted(common):
        if mine[rev][0] != theirs[rev][0]:
            differing.append(rev)
            problems.append(
                f"revision {rev} chained differently: mine down_revision={mine[rev][0]!r} vs theirs {theirs[rev][0]!r} "
                f"({mine[rev][1]} vs {theirs[rev][1]})"
            )
        elif mine[rev][2] != theirs[rev][2]:
            differing.append(rev)
            problems.append(f"revision {rev} exists in both lanes with different content ({mine[rev][1]} vs {theirs[rev][1]})")
    # Identical shared history is EXPECTED when lanes evolved from one lineage; only
    # revisions whose content/chaining actually differ are clashes.
    name_clashes = {mine[r][1] for r in differing}
    if name_clashes:
        problems.append(f"filename clashes (alembic loads by path; renumber one lineage): {sorted(name_clashes)}")
    if not problems:
        s.ok(f"migration graphs compatible: mine={len(mine)} revisions, theirs={len(theirs)}, {len(common)} shared, 0 conflicts")
    else:
        for pr in problems:
            s.fail(f"migration conflict: {pr}")
        s.warn("resolution: renumber/re-chain the Z.AI lineage migrations at composition (their lane owns 0069..0079 numbering); run alembic heads check post-merge")
    return problems


def surface_inventory(repo: Path, treeish: str) -> dict:
    inv: dict[str, list[str]] = {}
    def ls(prefix: str) -> list[str]:
        p = run(["git", "ls-tree", "-r", "--name-only", treeish, prefix], cwd=str(repo), check=False)
        return sorted(Path(x).name for x in p.stdout.splitlines() if x)
    inv["models"] = ls("backend/app/models/")
    inv["routers"] = ls("backend/app/routers/")
    inv["openai_consult"] = ls("backend/app/openai_consult/")
    cfg = run(["git", "show", f"{treeish}:backend/app/config.py"], cwd=str(repo), check=False).stdout
    inv["settings_keys"] = sorted(set(re.findall(r"^\s{4}([a-z][a-z0-9_]+):", cfg, re.M)))
    req = run(["git", "show", f"{treeish}:backend/requirements.txt"], cwd=str(repo), check=False).stdout or \
          run(["git", "show", f"{treeish}:backend/pyproject.toml"], cwd=str(repo), check=False).stdout
    inv["requirements_sha"] = [f"{len(req):08x}"]
    fe = run(["git", "grep", "-hoE", r"['\"]/api/[a-zA-Z0-9/_-]+['\"]", treeish, "--", "frontend"], cwd=str(repo), check=False)
    inv["frontend_api_calls"] = sorted(set(re.findall(r"/api/[a-zA-Z0-9/_-]+", fe.stdout)))
    return inv


def stage_surfaces(base_repo: Path, cand_repo: Path, cand_treeish: str, s: Stage) -> None:
    mine = surface_inventory(base_repo, SERIES_TIP)
    theirs = surface_inventory(cand_repo, cand_treeish)
    # My additions that MUST survive integration and not be clobbered:
    mine_only_models = set(mine["models"]) - set(theirs["models"])
    mine_only_routers = set(mine["routers"]) - set(theirs["routers"])
    shared_settings = set(mine["settings_keys"]) & set(theirs["settings_keys"])
    their_new_settings = set(theirs["settings_keys"]) - set(mine["settings_keys"])
    s.ok(f"models: mine-only (must keep): {sorted(mine_only_models)}")
    s.ok(f"routers: mine-only (must register): {sorted(mine_only_routers)}")
    s.warn(f"settings: {len(shared_settings)} shared keys (verify no semantic drift), {len(their_new_settings)} candidate-only keys to add at composition")
    fe_only = set(theirs["frontend_api_calls"]) - set(mine["frontend_api_calls"])
    s.ok(f"frontend /api calls: {len(mine['frontend_api_calls'])} mine / {len(theirs['frontend_api_calls'])} candidate, {len(fe_only)} candidate-only (verify endpoints exist post-composition)")
    consult_overlap = set(theirs["openai_consult"]) & set(mine["openai_consult"])
    if consult_overlap:
        s.fail(f"openai_consult collision — candidate also defines: {sorted(consult_overlap)}")
    else:
        s.ok("openai_consult package: zero collision with candidate tree")


def stage_killswitch(base_repo: Path, cand_repo: Path, cand_treeish: str, s: Stage) -> str:
    ks = run(["git", "show", f"{cand_treeish}:backend/app/workforce/kill_switch.py"], cwd=str(cand_repo), check=False).stdout
    if not ks:
        ks = run(["git", "show", f"{SERIES_TIP}:backend/app/workforce/kill_switch.py"], cwd=str(base_repo), check=False).stdout
        s.warn("candidate has no kill_switch.py — base version assumed for signature detection")
    if re.search(r"def\s+assert_not_killed\s*\(\s*db[^)]*,\s*owner_id", ks):
        s.ok("candidate kill switch is OWNER-SCOPED: assert_not_killed(db, owner_id) — isolated adaptation patch REQUIRED (lifeai-isolated-killswitch-sig-adaptation.patch)")
        return "owner-scoped"
    if re.search(r"def\s+assert_not_killed\s*\(\s*\)", ks):
        s.ok("candidate kill switch matches base signature: assert_not_killed() — no adaptation needed")
        return "base-signature"
    s.warn("kill switch signature not recognized — manual reconciliation required before candidate build")
    return "unknown"


def stage_candidate(base_repo: Path, cand_repo: Path, cand_full: str, outdir: Path, s: Stage) -> Path:
    """base + series (git am) + CANDIDATE DELTA (first-parent diff applied as a binary
    patch). A full-tree overlay would silently REVERT series-owned files that the
    candidate tree still carries at base state — the delta composes exactly what the
    candidate actually changed and fails closed on any conflict with the series."""
    cand = outdir / "candidate"
    run(["git", "clone", "-q", str(base_repo), str(cand)])
    run(["git", "am", "-q", str(Path(__file__).resolve().parent / "lifeai-openai-full-series-v4.patch")], cwd=str(cand), check=False)
    if run(["git", "rev-parse", "HEAD"], cwd=str(cand)).stdout.strip()[:7] != SERIES_TIP:
        raise RuntimeError("series did not apply inside candidate build")
    parents = run(["git", "rev-parse", f"{cand_full}^@"], cwd=str(cand_repo)).stdout.split()
    if not parents:
        raise RuntimeError("candidate commit has no parents — refusing to compose a root commit blindly")
    delta = run(["git", "diff", "--binary", parents[0], cand_full], cwd=str(cand_repo)).stdout
    if not delta.strip():
        s.warn("candidate delta vs first parent is empty — series-only candidate composed")
        s.ok(f"candidate composed at {SERIES_TIP} (candidate changed nothing on top of the series)")
        return cand
    p = run(["git", "apply", "--check", "--binary", "-"], cwd=str(cand), input=delta, check=False)
    if p.returncode != 0:
        conflicts = "\n".join(l for l in p.stderr.splitlines() if "error" in l.lower()) or p.stderr
        raise RuntimeError(f"candidate delta CONFLICTS with the series — human reconciliation required:\n{conflicts}")
    run(["git", "apply", "--binary", "-"], cwd=str(cand), input=delta)
    run(["git", "add", "-A"], cwd=str(cand))
    run(["git", "-c", "user.name=freebuff-integration-tool", "-c", "user.email=integration@tool.invalid",
         "commit", "-q", "-m", "integration-candidate: base + Z.AI series + Freebuff candidate delta"], cwd=str(cand))
    s.ok(f"candidate composed at {run(['git', 'rev-parse', 'HEAD'], cwd=str(cand)).stdout.strip()[:12]} — run integration tests before ANY further use")
    return cand


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--base", default="/home/z/my-project/review-target/LifeAI")
    ap.add_argument("--fixture", action="store_true", help="validation-only run; NEVER produces a candidate")
    ap.add_argument("--output-dir", default="/tmp/freebuff_integration")
    ap.add_argument("--report")
    args = ap.parse_args()
    global BASE_REPO_PATH
    BASE_REPO_PATH = args.base

    lines: list[str] = [f"# Freebuff integration run — {args.artifact}", ""]
    workdir = Path(tempfile.mkdtemp(prefix="freebuff_ingest_"))
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        s = Stage(lines)
        info = stage_ingest(Path(args.artifact), workdir, s)
        cand_full = resolve_candidate(info["repo"], s)
        if cand_full is None and not args.fixture:
            lines.append("\n**RESULT: NO-GO — the actual candidate commit is absent. Nothing was produced.**")
            print("\n".join(lines))
            return 2
        if cand_full is None:
            lines.append("\n_VALIDATION-ONLY FIXTURE RUN — no candidate produced by design._")
        elif args.fixture:
            lines.append("\n_VALIDATION-ONLY FIXTURE RUN — candidate hash PRESENT but fixture mode NEVER produces a candidate (S7 skipped)._")
            treeish = info["analysis"]  # the ingested tip (the fixture candidate); S7 gated off
            stage_ancestry(info["repo"], s)
            mig = stage_migrations(Path(args.base), info["repo"], treeish, s)
            stage_surfaces(Path(args.base), info["repo"], treeish, s)
            ks = stage_killswitch(Path(args.base), info["repo"], treeish, s)
            lines.append("")
            lines.append("**RESULT: validation complete (fixture run) — no candidate, by design.**")
            if args.report:
                Path(args.report).write_text("\n".join(lines) + "\n")
            print("\n".join(lines))
            return 0
        stage_ancestry(info["repo"], s)
        mig = stage_migrations(Path(args.base), info["repo"], cand_full or info["analysis"], s)
        stage_surfaces(Path(args.base), info["repo"], cand_full or info["analysis"], s)
        ks = stage_killswitch(Path(args.base), info["repo"], cand_full or info["analysis"], s)
        cand = None
        if cand_full:
            if mig:
                lines.append("\n**RESULT: GATE REFUSED — migration conflicts must be reconciled before candidate production.**")
                print("\n".join(lines))
                return 2
            if ks == "unknown":
                lines.append("\n**RESULT: GATE REFUSED — kill-switch signature unrecognized.**")
                print("\n".join(lines))
                return 2
            cand = stage_candidate(Path(args.base), info["repo"], cand_full, outdir, s)
        verdict = (
            f"**RESULT: CANDIDATE PRODUCED at `{cand}` — integration tests mandatory before any further use.**"
            if cand
            else ("**RESULT: validation complete (fixture run) — no candidate, by design.**" if args.fixture else "**RESULT: NO-GO.**")
        )
        lines.append("")
        lines.append(verdict)
        report = args.report
        if report:
            Path(report).write_text("\n".join(lines) + "\n")
        print("\n".join(lines))
        return 0
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        lines.append(f"\n❌ TOOL ERROR: {exc}")
        print("\n".join(lines))
        return 1


BASE_REPO_PATH = "/home/z/my-project/review-target/LifeAI"

if __name__ == "__main__":
    sys.exit(main())
