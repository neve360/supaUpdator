import aiohttp
import requests
import aiofiles
import asyncio
import json
import os
import io
import datetime
import shutil
import subprocess
from difflib import HtmlDiff
from typing import List
import htmlmin
from bs4 import BeautifulSoup
from github import Github, Repository, ContentFile
import yaml


async def download(
    c: ContentFile.ContentFile, out: str, session: aiohttp.ClientSession
):
    try:
        async with session.get(c.download_url) as res:
            output_path = os.path.join(out, c.path)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            async with aiofiles.open(output_path, "wb") as f:
                try:
                    print(f"downloading {c.path} to {output_path}")
                    while content := await res.content.read(20 << 10):
                        await f.write(content)
                except Exception as err:
                    raise Exception(f"Error writing file {c.name}: {err}")
                else:
                    return output_path
    except Exception as err:
        raise Exception(f"Error downloading {c.name}: {err}")


def get_repo_files(
    repo: Repository.Repository,
    folder: str,
    repoFiles: List[ContentFile.ContentFile],
    recursive: bool,
):
    contents = repo.get_contents(folder)
    for c in contents:
        if c.download_url is None:
            if recursive:
                get_repo_files(repo, c.path, repoFiles, recursive)
            continue
        repoFiles.append(c)


def split_image_ref(image: str) -> tuple[str, str | None]:
    image = image.strip()
    if "@" in image:
        repo, _digest = image.split("@", maxsplit=1)
        return repo, None

    last_slash = image.rfind("/")
    last_colon = image.rfind(":")

    if last_colon > last_slash:
        return image[:last_colon], image[last_colon + 1 :]

    return image, None


def check_minio_updates(file_path: str):
    with open(file_path) as f:
        services = yaml.safe_load(f).get("services", {})
        images: list[str] = []
        for service_name, service_data in services.items():
            if "minio" in service_name and isinstance(service_data, dict):
                image = service_data.get("image")
                if image:
                    images.append(str(image))
        data = ""

        for image in images:
            try:
                repo, current_tag = split_image_ref(image)
                if current_tag is None:
                    continue
                latest_tag = os.path.basename(
                    Github().get_repo(repo).get_latest_release().html_url
                )
                if current_tag != latest_tag:
                    data += f"<h2>{repo}: {latest_tag}</h2><br/>"
            except Exception as err:
                raise SystemExit(f"ERROR checking minio updates for {image}: {err}")

        return data


def get_local_file_path(remote_file_path: str, local_docker_dir: str) -> str:
    normalized_remote_path = os.path.normpath(remote_file_path)
    marker = f"docker{os.sep}"

    if marker in normalized_remote_path:
        relative_path = normalized_remote_path.split(marker, maxsplit=1).pop()
    else:
        path_parts = normalized_remote_path.split(os.sep)
        if "docker" not in path_parts:
            raise SystemExit(f"ERROR resolving local file path for {remote_file_path}")
        docker_index = path_parts.index("docker")
        relative_parts = path_parts[docker_index + 1 :]
        relative_path = os.path.join(*relative_parts)

    return os.path.normpath(os.path.join(local_docker_dir, relative_path))


def run_compose_command(local_docker_dir: str, *compose_args: str):
    cmd = ["docker", "compose", *compose_args]
    print(f"running {' '.join(cmd)} in {local_docker_dir}")

    try:
        res = subprocess.run(
            cmd,
            cwd=local_docker_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as err:
        output = "\n".join(x for x in [err.stdout, err.stderr] if x).strip()
        raise SystemExit(f"ERROR running {' '.join(cmd)}: {output or err}")

    if res.stdout:
        print(res.stdout.strip())
    if res.stderr:
        print(res.stderr.strip())


def apply_remote_updates(files_to_update: list[tuple[str, str]]):
    for remote_file_path, local_file_path in files_to_update:
        os.makedirs(os.path.dirname(local_file_path), exist_ok=True)
        shutil.copy2(remote_file_path, local_file_path)
        print(f"updated local file: {local_file_path}")


async def main():
    discord_webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if discord_webhook_url is None:
        raise SystemExit(
            "Error: DISCORD_WEBHOOK_URL env var not present",
        )

    current_dir = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(current_dir, "remote")
    local_docker_dir = #your path of supabase docker directory;
    if not os.path.isdir(local_docker_dir):
        raise SystemExit(f"Error: local_docker_dir not found: {local_docker_dir}")

    g = Github()
    repo = g.get_repo("supabase/supabase")
    repoFiles: List[ContentFile.ContentFile] = []
    get_repo_files(repo, "docker", repoFiles, True)

    remote_files = []

    async with aiohttp.ClientSession() as session:
        skip = ["readme.md", ".gitignore", "versions.md", "changelog.md"]
        try:
            async with asyncio.TaskGroup() as tg:
                for f in repoFiles:
                    if f.name.lower() in skip:
                        print(f"skip downloading {f.name}")
                    else:
                        remote_files.append(tg.create_task(download(f, out, session)))
        except* Exception as err:
            raise SystemExit(f"ERROR in download taskgroup: {err.exceptions}")

    remote_files = [
        os.path.normpath(remote_file.result()) for remote_file in remote_files
    ]

    extra_files: List[str] = []
    files_to_update: list[tuple[str, str]] = []
    html_head, html_body, minio_update = "", "", ""
    legends_table_html, legends_selector = "", "table[summary='Legends']"
    deployment_result = "<h2>No docker update applied.</h2>"

    for remote_file_path in remote_files:
        local_file_path = get_local_file_path(remote_file_path, local_docker_dir)

        if not os.path.isfile(local_file_path):
            extra_files.append(remote_file_path)
            files_to_update.append((remote_file_path, local_file_path))
            continue

        if os.path.basename(local_file_path) == "docker-compose.s3.yml":
            minio_update = check_minio_updates(local_file_path)

        with open(remote_file_path, "r") as remote_file:
            try:
                with open(local_file_path, "r") as local_file:
                    local_lines = local_file.read().splitlines()
                    remote_lines = remote_file.read().splitlines()

                    fileName = os.path.basename(local_file_path)

                    html_diff = HtmlDiff().make_file(
                        local_lines,
                        remote_lines,
                        fromdesc=f"{fileName} Local File",
                        todesc=f"{fileName} Remote File",
                        context=True,
                        numlines=10,
                    )
                    soup = BeautifulSoup(html_diff, "html.parser")

                    # fmt: off
                    if "no differences" in soup.select_one("body table tbody tr").get_text().lower():
                        continue
                    # fmt: on

                    files_to_update.append((remote_file_path, local_file_path))

                    if html_head == "":
                        html_head = soup.find("head").decode()

                    legends_table = soup.select_one(legends_selector)

                    if legends_table is not None:
                        if legends_table_html == "":
                            legends_table_html = legends_table.decode()
                        legends_table.decompose()

                    html_body += soup.find("body").decode_contents()
            except Exception as err:
                raise SystemExit(
                    f"Error generating diff for file {remote_file_path}: {err}"
                )

    if len(files_to_update) > 0:
        try:
            run_compose_command(local_docker_dir, "down")
            apply_remote_updates(files_to_update)
            run_compose_command(local_docker_dir, "pull")
            run_compose_command(local_docker_dir, "up", "-d")
        except Exception as err:
            raise SystemExit(f"ERROR applying docker updates: {err}")
        deployment_result = (
            f"<h2>Docker updated and restarted. Files synced: {len(files_to_update)}</h2>"
        )

    if len(html_body) == 0:
        html_diff = (
            f"<html><body><h1>No changes!</h1>{deployment_result}</body></html>"
        )
    else:
        html_diff = f"""
        <html>
        {html_head}
        <body>
        {deployment_result}
        {minio_update}
        {"" if len(extra_files) == 0 else f"<h1>extraFiles={str(extra_files)}</h1><br>"}
        {html_body}
        {legends_table_html}
        </body>
        </html>
        """

    report_date = datetime.datetime.now().strftime("%d-%m-%Y")

    file = io.StringIO(htmlmin.minify(html_diff, remove_empty_space=True))
    file.name = f"diff-{report_date}.html"

    try:
        res = requests.post(
            discord_webhook_url,
            data={
                "payload_json": json.dumps(
                    {"embeds": [{"title": f"Report {report_date}"}]}
                )
            },
            files={"file": file},
        )
        res.raise_for_status()
    except Exception as err:
        raise SystemExit(f"ERROR sending to discord webhook: {err}")
    finally:
        file.close()


if __name__ == "__main__":
    asyncio.run(main())
