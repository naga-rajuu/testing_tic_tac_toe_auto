import argparse
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict
from urllib import error, request

from langgraph.graph import END, START, StateGraph

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "repo_config.json"
DEFAULT_LOCAL_BASE_DIR = Path.home() / "Downloads"
DEFAULT_CONFIG = {
    "github_owner": "naga-rajuu",
    "github_repo": "testing_tic_tac_toe_auto",
    "repo_visibility": "public",
    "github_token_env": "GITHUB_TOKEN",
    "local_base_dir": str(DEFAULT_LOCAL_BASE_DIR),
    "model": "llama3:latest",
}


SYSTEM_PROMPT = """
You are a file creation assistant.

The user will give a command or sentence.
Always call the available tool to:
1. Create a new text file containing the user's exact command.
2. Commit the new file to git.
3. Push the commit to the configured remote repository.

Do not answer without using the tool unless the user asks a question unrelated to file creation.
""".strip()

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_text_file_and_push",
        "description": "Create a new text file with the user's command, commit it, and push it to git.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The user's exact command text to save inside the new text file.",
                }
            },
            "required": ["command"],
        },
    },
}


class AgentState(TypedDict):
    messages: list[dict[str, Any]]
    repo_path: str
    model: str
    command_text: str


def run_git_command(repo_root: Path, args: list[str], check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        error_text = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {error_text}")
    return (result.stdout or result.stderr).strip()


def resolve_repo_root(repo_path: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error_text = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"{repo_path} is not a git repository. Details: {error_text}"
        )
    return Path(result.stdout.strip())


def make_safe_stem(command: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", command.strip()).strip("_").lower()
    if not cleaned:
        return "command"
    return cleaned[:40]


def load_repo_config() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Missing config file: {CONFIG_PATH}. Copy repo_config.example.json to repo_config.json and fill it in."
        )
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    missing = [key for key in DEFAULT_CONFIG if key not in config or not config[key]]
    if missing:
        raise RuntimeError(
            f"Config file {CONFIG_PATH} is missing required keys: {', '.join(missing)}"
        )
    return config


def github_api_request(
    method: str,
    api_path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"https://api.github.com{api_path}",
        data=body,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with request.urlopen(req, timeout=120) as response:
            response_body = response.read().decode("utf-8")
            return response.status, json.loads(response_body) if response_body else {}
    except error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        parsed_body: dict[str, Any]
        try:
            parsed_body = json.loads(response_body) if response_body else {}
        except json.JSONDecodeError:
            parsed_body = {"message": response_body}
        return exc.code, parsed_body


def ensure_remote_repo_exists(config: dict[str, str]) -> str:
    token = os.environ.get(config["github_token_env"], "").strip()
    if not token:
        raise RuntimeError(
            f"Set the {config['github_token_env']} environment variable with a GitHub token that can create repositories."
        )

    owner = config["github_owner"]
    repo = config["github_repo"]
    status, response = github_api_request("GET", f"/repos/{owner}/{repo}", token)
    if status == 200:
        return str(response["clone_url"])
    if status != 404:
        message = response.get("message", "Unknown GitHub API error")
        raise RuntimeError(f"Failed to check GitHub repo {owner}/{repo}: {message}")

    create_status, create_response = github_api_request(
        "POST",
        "/user/repos",
        token,
        {
            "name": repo,
            "private": config["repo_visibility"].lower() == "private",
        },
    )
    if create_status not in (201, 422):
        message = create_response.get("message", "Unknown GitHub API error")
        raise RuntimeError(f"Failed to create GitHub repo {owner}/{repo}: {message}")

    if create_status == 422:
        recheck_status, recheck_response = github_api_request(
            "GET", f"/repos/{owner}/{repo}", token
        )
        if recheck_status != 200:
            message = recheck_response.get("message", "Repository may exist under a different owner")
            raise RuntimeError(f"Could not confirm existing repo {owner}/{repo}: {message}")
        return str(recheck_response["clone_url"])

    return str(create_response["clone_url"])


def ensure_local_repo(clone_url: str, config: dict[str, str]) -> Path:
    local_base_dir = Path(config["local_base_dir"]).expanduser().resolve()
    local_base_dir.mkdir(parents=True, exist_ok=True)
    repo_path = local_base_dir / config["github_repo"]

    if repo_path.exists():
        return resolve_repo_root(repo_path)

    run_git_command(local_base_dir, ["clone", clone_url, str(repo_path)])
    return resolve_repo_root(repo_path)


def create_text_file_and_push(repo_path: Path, command: str) -> str:
    repo_root = resolve_repo_root(repo_path)
    output_dir = repo_root / "generated_commands"
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = make_safe_stem(command)
    file_path = output_dir / f"{timestamp}_{stem}.txt"
    file_path.write_text(command + "\n", encoding="utf-8")

    relative_path = file_path.relative_to(repo_root)
    run_git_command(repo_root, ["add", str(relative_path)])
    commit_message = f"Add command file for: {command[:50]}"
    commit_output = run_git_command(repo_root, ["commit", "-m", commit_message])
    push_output = run_git_command(repo_root, ["push", "-u", "origin", "HEAD"])

    return (
        f"Created {relative_path} with the command text.\n"
        f"Commit result: {commit_output}\n"
        f"Push result: {push_output}"
    )


def call_ollama(model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "tools": [TOOL_SCHEMA],
        }
    ).encode("utf-8")
    req = request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Ollama API request failed with status {exc.code}: {error_body}"
        ) from exc


def build_fallback_tool_call(command_text: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "function": {
                    "name": "create_text_file_and_push",
                    "arguments": {"command": command_text},
                }
            }
        ],
    }


def llm_node(state: AgentState) -> AgentState:
    try:
        response = call_ollama(state["model"], state["messages"])
        next_message = response["message"]
    except RuntimeError as exc:
        if "does not support tools" in str(exc):
            next_message = build_fallback_tool_call(state["command_text"])
        else:
            raise
    updated_messages = state["messages"] + [next_message]
    return {
        "messages": updated_messages,
        "repo_path": state["repo_path"],
        "model": state["model"],
        "command_text": state["command_text"],
    }


def tool_node(state: AgentState) -> AgentState:
    last_message = state["messages"][-1]
    tool_call = last_message["tool_calls"][0]
    args = tool_call.get("function", {}).get("arguments", {})
    if isinstance(args, str):
        args = json.loads(args)
    command = args["command"]
    tool_result = create_text_file_and_push(Path(state["repo_path"]), command)
    tool_message = {
        "role": "tool",
        "name": "create_text_file_and_push",
        "content": tool_result,
    }
    return {
        "messages": state["messages"] + [tool_message],
        "repo_path": state["repo_path"],
        "model": state["model"],
        "command_text": state["command_text"],
    }


def should_run_tool(state: AgentState) -> str:
    last_message = state["messages"][-1]
    if last_message.get("tool_calls"):
        return "tool"
    return "end"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("llm", llm_node)
    graph.add_node("tool", tool_node)
    graph.add_edge(START, "llm")
    graph.add_conditional_edges(
        "llm",
        should_run_tool,
        {"tool": "tool", "end": END},
    )
    graph.add_edge("tool", END)
    return graph.compile()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a text file from a command and push it using LangGraph + Ollama."
    )
    parser.add_argument("command", help="The command text to save in a new file.")
    parser.add_argument(
        "--repo-path",
        default=None,
        help="Optional local repo path override. If omitted, the app uses repo_config.json and creates/clones the repo automatically.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional Ollama model override.",
    )
    args = parser.parse_args()

    config = load_repo_config()
    clone_url = ensure_remote_repo_exists(config)
    if args.repo_path:
        repo_path = Path(args.repo_path).resolve()
    else:
        repo_path = ensure_local_repo(clone_url, config)
    model_name = args.model or config["model"]
    app = build_graph()
    initial_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": args.command},
    ]
    try:
        result = app.invoke(
            {
                "messages": initial_messages,
                "repo_path": str(repo_path),
                "model": model_name,
                "command_text": args.command,
            }
        )
        final_message = result["messages"][-1]["content"]
        print(final_message)
    except Exception as exc:
        print(f"Error: {exc}")


if __name__ == "__main__":
    main()
