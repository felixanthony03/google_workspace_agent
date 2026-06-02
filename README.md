# AI Workspace Agent Suite

AI-powered agents for automating Gmail refund processing and Google Calendar management using LangChain, LangGraph, and MCP.

## Prerequisites

- Python 3.12+
- uv package manager
- Google Cloud project with Gmail and Calendar APIs enabled
- OAuth 2.0 credentials (Desktop application type)
- OpenAI API key

## Setup

### 1. Clone and sync project
```bash
 uv sync 
```

### 2. Install workspace-mcp
```bash
uv pip install workspace-mcp
```
### 3. Configure environment
```bash
cp .env.example .env

Edit .env with your credentials:

OPENAI_API_KEY=sk-...
GOOGLE_OAUTH_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=GOCSPX-...
OAUTHLIB_INSECURE_TRANSPORT=1
```

## Usage

### Refund Agent

Process unread refund, return, and complaint emails automatically:
```bash
uv run workspace_agent_suite.py refund --mode auto --email your-email@gmail.com
```
Interactive chat mode:
```bash
uv run workspace_agent_suite.py refund --mode interactive --email your-email@gmail.com
```
### Calendar Agent

Run demo queries:
```bash
uv run workspace_agent_suite.py calendar --mode demo --email your-email@gmail.com
```
Interactive chat mode:
```bash
uv run workspace_agent_suite.py calendar --mode interactive --email your-email@gmail.com
```
## Modes

- auto - Fully automated processing (refund agent only)
- interactive - Chat-based interaction with the agent
- demo - Run predefined example queries (calendar agent only)

## Architecture

The suite starts an MCP server (workspace-mcp) locally on port 8000, then connects LangGraph agents to it. Refund agent uses MCP tools for Gmail operations. Calendar agent combines MCP tools for write operations with CLI tools for fast read queries.

## Important Notes

Authorization is required for the first when the script was run follow what the prompt says. 
If the tool was stuck, please re-run the script and exit to ensure the server was terminated properly before trying again. 