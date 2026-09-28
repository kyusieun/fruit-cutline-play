#!/usr/bin/env python3
"""Web artifact 명세 생성과 Pages 수신. Python 표준 라이브러리와 gh만 사용한다."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile

SOURCE = "kyusieun/fruit-cutline"
WORKFLOW = ".github/workflows/deploy-web.yml"
REQUIRED = {"index.html", "index.js", "index.pck", "index.wasm"}
MAX_BYTES = 1024 ** 3


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def valid_name(name):
    return isinstance(name, str) and re.fullmatch(r"index[\w.-]*", name, re.ASCII)


def artifact_name(manifest):
    return f"fruit-cutline-web-{manifest['run_id']}-{manifest['run_attempt']}"


def validate_manifest(manifest):
    require(isinstance(manifest, dict), "명세는 JSON object여야 합니다.")
    require(manifest.get("schema_version") == 1, "지원하지 않는 명세 버전입니다.")
    require(manifest.get("source_repository") == SOURCE, "허용하지 않은 소스 저장소입니다.")
    require(re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_sha", ""))), "잘못된 source SHA입니다.")
    for field in ("run_id", "run_attempt", "artifact_id"):
        value = manifest.get(field)
        require(type(value) is int and value > 0, f"잘못된 {field}입니다.")
    require(manifest.get("artifact_name") == artifact_name(manifest), "artifact 이름이 실행과 다릅니다.")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", str(manifest.get("artifact_digest", ""))), "잘못된 artifact digest입니다.")
    files = manifest.get("files")
    require(isinstance(files, dict) and REQUIRED <= files.keys(), "필수 Web 파일이 없습니다.")
    total = 0
    for name, entry in files.items():
        require(valid_name(name) and isinstance(entry, dict), "root index* 파일만 허용합니다.")
        size = entry.get("size")
        require(type(size) is int and size > 0, f"잘못된 파일 크기: {name}")
        require(re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))), f"잘못된 파일 hash: {name}")
        total += size
    require(total <= MAX_BYTES, "Web 파일 합계가 Pages의 1GiB 한도를 넘습니다.")
    return manifest


def web_files(directory):
    files = sorted(Path(directory).glob("index*"))
    require(REQUIRED <= {path.name for path in files}, "필수 Web 파일이 없습니다.")
    for path in files:
        require(valid_name(path.name) and path.is_file() and not path.is_symlink(), "일반 root index* 파일만 허용합니다.")
        require(path.stat().st_size > 0, f"빈 Web 파일: {path.name}")
    return files


def create_manifest(directory, destination):
    require(os.environ.get("GITHUB_REPOSITORY") == SOURCE, "소스 저장소가 다릅니다.")
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "main 빌드만 게시합니다.")
    manifest = {
        "schema_version": 1,
        "source_repository": SOURCE,
        "source_sha": os.environ["GITHUB_SHA"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "artifact_id": int(os.environ["WEB_ARTIFACT_ID"]),
        "artifact_digest": "sha256:" + os.environ["WEB_ARTIFACT_DIGEST"].removeprefix("sha256:"),
        "files": {path.name: {"size": path.stat().st_size, "sha256": sha256(path)} for path in web_files(directory)},
    }
    manifest["artifact_name"] = artifact_name(manifest)
    validate_manifest(manifest)
    Path(destination).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def api_json(endpoint):
    result = subprocess.run(["gh", "api", endpoint], check=True, stdout=subprocess.PIPE, text=True, timeout=60)
    return json.loads(result.stdout)


def validate_run(run, manifest):
    require(run.get("id") == manifest["run_id"] and run.get("run_attempt") == manifest["run_attempt"], "소스 실행 ID/attempt 불일치입니다.")
    require(run.get("head_sha") == manifest["source_sha"] and run.get("head_branch") == "main", "소스 SHA/branch 불일치입니다.")
    require(run.get("path") == WORKFLOW and run.get("event") in ("push", "workflow_dispatch"), "허용하지 않은 workflow/event입니다.")
    for field in ("repository", "head_repository"):
        require(run.get(field, {}).get("full_name") == SOURCE, "다른 저장소 또는 fork의 빌드입니다.")


def wait_for_success(manifest, wait_seconds=180):
    # 명세 push 뒤 upstream post step이 끝날 때까지 기다린다. 실패/취소에는 재시도하지 않는다.
    endpoint = f"repos/{SOURCE}/actions/runs/{manifest['run_id']}/attempts/{manifest['run_attempt']}"
    deadline = time.monotonic() + wait_seconds
    while True:
        run = api_json(endpoint)
        validate_run(run, manifest)
        if run.get("status") == "completed":
            require(run.get("conclusion") == "success", "소스 실행이 실패 또는 취소되었습니다.")
            return run
        require(time.monotonic() < deadline, "소스 실행 완료 대기시간을 초과했습니다.")
        time.sleep(5)


def validate_artifact(artifact, manifest, run):
    require(artifact.get("id") == manifest["artifact_id"], "artifact ID 불일치입니다.")
    require(artifact.get("name") == manifest["artifact_name"], "artifact 이름 불일치입니다.")
    require(artifact.get("expired") is False, "artifact가 만료되었습니다. 재빌드가 필요합니다.")
    require(artifact.get("digest") == manifest["artifact_digest"], "artifact digest 불일치입니다.")
    origin = artifact.get("workflow_run", {})
    require(origin.get("id") == manifest["run_id"] and origin.get("head_sha") == manifest["source_sha"], "artifact의 소스 실행 불일치입니다.")
    require(origin.get("head_branch") == "main", "artifact가 main 빌드가 아닙니다.")
    source_id = run["repository"]["id"]
    require(origin.get("repository_id") == source_id and origin.get("head_repository_id") == source_id, "artifact의 소스 저장소 불일치입니다.")


def extract_verified(archive, destination, manifest):
    require("sha256:" + sha256(archive) == manifest["artifact_digest"], "다운로드한 ZIP digest 불일치입니다.")
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        require(len(entries) == len(manifest["files"]), "ZIP 항목 수가 다릅니다.")
        require({entry.filename for entry in entries} == manifest["files"].keys(), "ZIP 파일 목록이 다릅니다.")
        for entry in entries:
            mode = stat.S_IFMT(entry.external_attr >> 16)
            require(not entry.is_dir() and mode in (0, stat.S_IFREG), "링크와 디렉터리는 배포하지 않습니다.")
            require(entry.file_size == manifest["files"][entry.filename]["size"], "ZIP 파일 크기가 다릅니다.")
        destination = Path(destination)
        destination.mkdir()  # 기존 site 디렉터리를 덮어쓰지 않는다.
        for entry in entries:
            target = destination / entry.filename
            with bundle.open(entry) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            require(sha256(target) == manifest["files"][entry.filename]["sha256"], f"파일 hash 불일치: {entry.filename}")
    (destination / ".nojekyll").touch()


def fetch_site(manifest_path, destination):
    manifest = validate_manifest(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
    require(bool(os.environ.get("GH_TOKEN")), "WEB_ARTIFACT_READ_TOKEN이 필요합니다.")
    run = wait_for_success(manifest)
    endpoint = f"repos/{SOURCE}/actions/artifacts/{manifest['artifact_id']}"
    validate_artifact(api_json(endpoint), manifest, run)
    with tempfile.TemporaryDirectory(prefix="fruit-pages-") as scratch:
        archive = Path(scratch) / "web.zip"
        with archive.open("wb") as output:
            subprocess.run(["gh", "api", endpoint + "/zip"], stdout=output, check=True, timeout=180)
        extract_verified(archive, destination, manifest)
    print(f"검증 완료: source {manifest['source_sha']}, run {manifest['run_id']}/{manifest['run_attempt']}, artifact {manifest['artifact_id']}")


def stage_legacy(directory, destination):
    files = web_files(directory)
    Path(destination).mkdir()
    for path in files:
        shutil.copyfile(path, Path(destination) / path.name)
    (Path(destination) / ".nojekyll").touch()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "fetch", "legacy"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    {"create": create_manifest, "fetch": fetch_site, "legacy": stage_legacy}[args.command](args.source, args.destination)


if __name__ == "__main__":
    main()
