# LangGraph Git File Creator

This project uses:

- Python
- LangGraph
- Ollama with `llama3:latest` through the local HTTP API
- a tool call that creates a text file and pushes it to git

## What it does

When you run the app with a command:

1. LangGraph sends your command to the local LLM.
2. The agent calls a tool.
3. The tool creates a new `.txt` file in `generated_commands/`.
4. The tool runs `git add`, `git commit`, and `git push`.

## Important

The app now supports a one-command flow:

- create the GitHub repo remotely if it does not exist
- clone it locally if it is missing
- create the text file
- commit and push it

To do that, it reads settings from `repo_config.json` and expects a GitHub token in the environment variable named by `github_token_env`.

## Install

```powershell
python -m pip install --user -r requirements.txt
```

Make sure Ollama is running and `llama3:latest` is available.

Set your GitHub token before running:

```powershell
$env:GITHUB_TOKEN="your-github-token"
```

Edit `repo_config.json` to choose:

- GitHub owner
- repo name
- public or private visibility
- local clone directory
- Ollama model

## Run

```powershell
python app.py "create a note about project kickoff"
```

Optional override if you want to target a specific existing local repo:

```powershell
python app.py "create a note about project kickoff" --repo-path "C:\path\to\your\local\repo"
```

## Output

The tool will create files like:

```text
generated_commands/20260512_200000_create_a_note_about_project_kickoff.txt
```

That file will contain the exact command text you passed in.
