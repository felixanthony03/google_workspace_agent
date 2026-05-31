"""AI Workspace Agent Suite

A reference implementation for the project described in the PDF.

Run examples:
    python workspace_agent_suite.py refund --mode auto --email user@gmail.com
    python workspace_agent_suite.py calendar --mode interactive --email user@gmail.com
    python workspace_agent_suite.py calendar --mode demo --email user@gmail.com

Environment variables required:
    OPENAI_API_KEY
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET

Optional:
    OAUTHLIB_INSECURE_TRANSPORT=1   # local development only
"""
from __future__ import annotations
import atexit
import time
from datetime import datetime, timezone, timedelta
import subprocess as sp
import argparse
import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Annotated, Sequence, TypedDict
from rich.console import Console
from rich.markdown import Markdown

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

try:
    from langgraph.graph.message import add_messages
except Exception:  # pragma: no cover
    from langgraph.graph import add_messages  # type: ignore

from langchain_openai import ChatOpenAI

try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
except Exception as exc:  # pragma: no cover
    raise ImportError(
        "Could not import MultiServerMCPClient from langchain_mcp_adapters. "
        "Please install the package versions expected by your environment."
    ) from exc

from dotenv import load_dotenv

load_dotenv()

console = Console()

# Taipei timezone (UTC+8, no DST)
TAIPEI_TZ = timezone(timedelta(hours=8))
MCP_SERVER_PORT = 8000
current_time = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


def _require_env(names: list[str]) -> list[str]:
    return [name for name in names if not os.getenv(name)]


def _print_setup_guide() -> None:
    print(
        """
Missing required environment variables.

Set these before running the agents:
    export OPENAI_API_KEY=sk-...
    export GOOGLE_OAUTH_CLIENT_ID=<your-client-id>
    export GOOGLE_OAUTH_CLIENT_SECRET=<your-client-secret>

Optional for local development only:
    export OAUTHLIB_INSECURE_TRANSPORT=1

Also make sure workspace-cli works:
    workspace-cli list
    workspace-cli call list_calendars
""".strip()
    )


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _humanize_json(obj: Any) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)

import time
import httpx

def start_mcp_server(port: int = MCP_SERVER_PORT) -> subprocess.Popen:
    """Start MCP server with streamable-http transport"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    
    # Pass OAuth credentials to the server
    env["GOOGLE_OAUTH_CLIENT_ID"] = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    env["GOOGLE_OAUTH_CLIENT_SECRET"] = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")

    proc = subprocess.Popen(
        [
            "uvx", "workspace-mcp",
            "--transport", "streamable-http",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    
    # Register cleanup
    def cleanup():
        if proc.poll() is None:
            print("Stopping MCP server...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    
    atexit.register(cleanup)
    
    # Wait for server to be ready - poll the /mcp endpoint
    print(f"Starting MCP server on port {port}...")
    url = f"http://localhost:{port}/mcp"
    
    for i in range(30):  # Try for up to 30 seconds
        try:
            # Try to connect to the server
            response = httpx.get(url, timeout=1.0)
            print(f"✓ MCP server responded with status {response.status_code}")
            break
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if proc.poll() is not None:
                # Process died, get error
                _, stderr = proc.communicate()
                raise RuntimeError(f"MCP server crashed: {stderr.decode()}")
            
            if i % 5 == 0:  # Print progress every 5 seconds
                print(f"  Waiting for server... ({i}s)")
            time.sleep(1)
    else:
        # Timed out
        proc.terminate()
        raise RuntimeError("MCP server failed to start within 30 seconds")
    
    print("✓ MCP server is ready!")
    return proc


# REFUND_SYSTEM_PROMPT = """You are a customer support agent for refund and return emails.
# Your email account is: {user_email}

# CRITICAL: You MUST process ALL matching emails, not just one. Never stop after processing a single email.

# WORKFLOW:
# 1. SEARCH: Use search_gmail_messages to find ALL unread refund/return/complaint emails.
# 2. COUNT: Determine how many emails were found.
# 3. BATCH READ: Use get_gmail_messages_content_batch to read ALL found emails at once (if available).
#    If batch read isn't available, use get_gmail_message_content for each one sequentially.
# 4. PROCESS EACH: For every single email found:
#    a. Extract sender email from 'From' field
#    b. Classify: REFUND_REQUEST, RETURN_REQUEST, COMPLAINT, OTHER
#    c. For REFUND/RETURN/COMPLAINT: Send reply using send_gmail_message
#    d. For OTHER: Skip
# 5. VERIFY: After processing, confirm that ALL emails were handled.

# CRITICAL REPLY RULES:
# - The 'to' field must be the CUSTOMER's email (From field), NEVER {user_email}
# - Use the thread_id from the original message
# - Never stop until ALL emails are processed

# Available tools:
# - search_gmail_messages: Search for emails
# - get_gmail_message_content: Read a single email
# - get_gmail_messages_content_batch: Read MULTIPLE emails at once (use this!)
# - send_gmail_message: Send a reply
# - draft_gmail_message: Create a draft
# - get_gmail_thread_content: Read an email thread

# Reply templates:
# - REFUND_REQUEST: Acknowledge refund, mention 3-5 business days
# - RETURN_REQUEST: Acknowledge return, provide return instructions
# - COMPLAINT: Apologize, promise follow-up within 24 hours

# IMPORTANT: After processing the first email, check if there are MORE emails to process.
# Do not stop until you've processed ALL emails found in the initial search.
# """

REFUND_SYSTEM_PROMPT = """You are a customer support agent for refund and return emails.
Your email account is: {user_email}

CRITICAL: You MUST process ALL matching emails, not just one. Never stop after processing a single email.

WORKFLOW:
1. SEARCH: Use search_gmail_messages to find ALL unread refund/return/complaint emails.
2. COUNT: Determine how many emails were found.
3. BATCH READ: Use get_gmail_messages_content_batch to read ALL found emails at once (if available).
   If batch read isn't available, use get_gmail_message_content for each one sequentially.
4. PROCESS EACH: For every single email found:
   a. Extract sender email from 'From' field
   b. Classify: REFUND_REQUEST, RETURN_REQUEST, COMPLAINT, OTHER
   c. For REFUND/RETURN/COMPLAINT: 
      - Send reply using send_gmail_message
      - Mark the original email as read using modify_gmail_message_labels
   d. For OTHER: 
      - Mark as read using modify_gmail_message_labels (no reply needed)
5. VERIFY: After processing, confirm that ALL emails were handled and marked as read.

CRITICAL REPLY RULES:
- The 'to' field must be the CUSTOMER's email (From field), NEVER {user_email}
- Use the thread_id from the original message when replying
- Never stop until ALL emails are processed AND marked as read
- Always mark emails as read after processing to avoid re-processing them

Available tools:
- search_gmail_messages: Search for emails
- get_gmail_message_content: Read a single email
- get_gmail_messages_content_batch: Read MULTIPLE emails at once (use this!)
- send_gmail_message: Send a reply
- draft_gmail_message: Create a draft (fallback if send fails)
- get_gmail_thread_content: Read an email thread
- modify_gmail_message_labels: Modify labels on a single message (use to mark as read)
- batch_modify_gmail_message_labels: Modify labels on MULTIPLE messages at once (use to mark all as read)

MARKING EMAILS AS READ:
- After replying to an email, immediately mark it as read using modify_gmail_message_labels
- Remove the 'UNREAD' label from each processed message
- If processing many emails, use batch_modify_gmail_message_labels to mark all as read at once

Reply templates:
- REFUND_REQUEST: Acknowledge the refund request, confirm it will be processed, mention 3-5 business days
- RETURN_REQUEST: Acknowledge the return request, provide clear return instructions
- COMPLAINT: Apologize sincerely, acknowledge the issue, promise follow-up within 24 hours

SUMMARY: After processing all emails, provide a summary showing:
- Total emails found
- How many were REFUND_REQUEST, RETURN_REQUEST, COMPLAINT, and OTHER
- Confirmation that all processed emails were marked as read
- For each email: sender, classification, and action taken

IMPORTANT: After processing the first email, check if there are MORE emails to process.
Do not stop until you've processed AND marked as read ALL emails found in the initial search.
"""

CALENDAR_SYSTEM_PROMPT = """You are a helpful Google Calendar assistant.

User Account:
- Your user's email is: {user_email}
- Use this email for all calendar operations and tool calls.
- If the user asks to switch accounts, update the email accordingly.

Behavior:
- Use CLI tools for simple read-only queries such as today's events, event lists, and calendar discovery.
- Use MCP tools for create, update, delete, RSVP, or more complex calendar operations.
- For destructive actions like update or delete, ask for explicit confirmation before proceeding.
- Present times in a human readable form whenever possible.

CRITICAL: When passing IDs between tools, ALWAYS copy them EXACTLY from the previous tool output. 
Never rewrite, retype, or modify an event_id or calendar_id. These are opaque strings that must be preserved character-for-character.
- Event IDs look like: 2urbhv336hlgtm5nuj0vdo33m6_20260523T020000Z
- Never truncate, shorten, or modify these IDs.

Time:
- Assume all times are in Taipei timezone (UTC+8) unless specified otherwise.
- Current time is {current_time} (Taipei time).
"""

def _utc_iso(dt: datetime) -> str:
    """Convert datetime to UTC ISO format string"""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _taipei_iso(dt: datetime) -> str:
    """Convert datetime to Taipei time ISO format string"""
    return dt.astimezone(TAIPEI_TZ).replace(microsecond=0).isoformat()


def _run_cli(args: list[str], timeout: int = 60) -> dict[str, Any] | str:
    cmd = [
        "workspace-cli",
        "--url", f"http://localhost:{MCP_SERVER_PORT}/mcp",  # Add this line
        *args
    ]
    
    # Fix for Windows Unicode issues
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        return {"error": "workspace-cli not found", "details": str(exc)}
    except subprocess.TimeoutExpired:
        return {"error": "workspace-cli timed out", "timeout_seconds": timeout}

    if proc.returncode != 0:
        full_error = ""
        if proc.stdout:
            full_error += proc.stdout.strip() + "\n"
        if proc.stderr:
            full_error += proc.stderr.strip()
        
        return {
            "error": "workspace-cli returned non-zero exit code",
            "details": full_error.strip(),
            "return_code": proc.returncode
        }

    stdout = proc.stdout.strip() if proc.stdout else ""
    stderr = proc.stderr.strip() if proc.stderr else ""
    
    if not stdout and not stderr:
        return {"error": "workspace-cli returned empty output", "details": "No output from command"}

    # Some tools might output to stderr
    output = stdout if stdout else stderr
    
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output

# def _run_cli(args: list[str], timeout: int = 60):
#     cmd = ["workspace-cli", *args]
#     try:
#         proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
#     except subprocess.TimeoutExpired:
#         return {"error": "timed out"}
#     if proc.returncode != 0:
#         return {"error": proc.stderr.strip()}
#     try:
#         return json.loads(proc.stdout)
#     except json.JSONDecodeError:
#         return proc.stdout.strip()

@tool
def cli_today_events(user_email: str, calendar_id: str = "primary") -> dict[str, Any] | str:
    """List today's calendar events (Taipei time) using workspace-cli."""
    now = datetime.now(TAIPEI_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    
    output = _run_cli(
        [
            "call",
            "get_events",
            f"user_google_email={user_email}",
            f"calendar_id={calendar_id}",
            f"time_min={_taipei_iso(start)}",
            f"time_max={_taipei_iso(end)}",
        ]
    )

    # print(output)
    return output


@tool
def cli_list_events(
    user_email: str,
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
) -> dict[str, Any] | str:
    """List calendar events in a specific time range (Taipei time)."""
    output = _run_cli(
        [
            "call",
            "get_events",
            f"user_google_email={user_email}",
            f"calendar_id={calendar_id}",
            f"time_min={time_min}",
            f"time_max={time_max}",
        ]
    )
    # print(output)
    return output

@tool
def cli_list_calendars(email: str) -> dict[str, Any] | str:
    """List all calendars using workspace-cli."""

    output = _run_cli(["call", "list_calendars", f"user_google_email={email}"])
    # print(output)
    return output


@tool
def cli_get_event(user_email: str, event_id: str, calendar_id: str = "primary") -> dict[str, Any] | str:
    """Fetch a single calendar event by ID using workspace-cli. Make sure to provide complete event_id."""

    output = _run_cli(
        [
            "call",
            "get_events",
            f"user_google_email={user_email}",
            f"calendar_id={calendar_id}",
            f"event_id={event_id}",
        ]
    )

    # print(output)
    return output


@tool
def cli_tool_list() -> dict[str, Any] | str:
    """List available workspace-cli tools for debugging."""

    output = _run_cli(["list"])
    # print(output)
    return output


MCP_BASE_CONFIG  = {
        "google-workspace": {
            "transport": "streamable-http",
            "url": f"http://localhost:{MCP_SERVER_PORT}/mcp",

        }
    }


async def _build_refund_agent(mcp_client: MultiServerMCPClient, user_email: str):
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    mcp_tools = await mcp_client.get_tools()

    # print("\nAvailable MCP tools for refund agent:")
    # for t in mcp_tools:
    #     print(f"  - {getattr(t, 'name', 'unknown')}")
    # print()

    refund_tool_names = {
        "search_gmail_messages",
        "get_gmail_message_content",
        "get_gmail_messages_content_batch",
        "send_gmail_message",
        "draft_gmail_message",
        "get_gmail_thread_content",
        "get_gmail_threads_content_batch",      # ← ADD THIS (batch thread reading)
        "list_gmail_labels",
        "modify_gmail_message_labels",          # Mark single email as read
        "batch_modify_gmail_message_labels",    # ← ADD THIS (mark ALL as read at once)
    }

    # check if tools exist in mcp
    for name in refund_tool_names:
        if not any(getattr(t, "name", "") == name for t in mcp_tools):
            print(f"Warning: Expected tool '{name}' not found in MCP tools. Make sure it is registered and available.")
    tools = [t for t in mcp_tools if getattr(t, "name", "") in refund_tool_names]
    print("Refund agent will use the following tools:")
    for tool in tools:
        print(f"  - {tool.name}")
    llm = llm.bind_tools(tools)

    # Format system prompt with user email
    system_prompt = REFUND_SYSTEM_PROMPT.format(user_email=user_email)
    print(f"\nUsing system prompt with email: {user_email}\n")

    def agent_node(state: AgentState) -> AgentState:
        messages = [SystemMessage(content=system_prompt), *list(state["messages"])]
        response = llm.invoke(messages)
        if hasattr(response, 'tool_calls') and response.tool_calls:
            print("\n🔧 TOOL CALLS:")
            for tc in response.tool_calls:
                print(f"  → {tc['name']}")
                print(f"    Args: {json.dumps(tc['args'], indent=4)}")
            print()
        return {"messages": [response]}

    def should_continue(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)

    tool_node = ToolNode(tools)

    graph.add_node("tools", tool_node)

    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


async def _build_calendar_agent(mcp_client: MultiServerMCPClient, user_email: str):
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    mcp_tools = await mcp_client.get_tools()

    # # DEBUG: Print all tool names from the server
    # print("\n" + "="*60)
    # print(f"ALL MCP TOOLS FROM SERVER ({len(mcp_tools)} total):")
    # print("="*60)
    # all_tool_names = sorted([t.name for t in mcp_tools])
    # for name in all_tool_names:
    #     print(f"  - {name}")
    # print("="*60 + "\n")
    
    # # Check which calendar tools actually exist
    # calendar_keywords = ["calendar", "event", "freebusy", "focus", "office"]
    # print("CALENDAR-RELATED TOOLS FOUND:")
    # for name in all_tool_names:
    #     if any(kw in name.lower() for kw in calendar_keywords):
    #         print(f"  ✓ {name}")
    # print()


    calendar_tool_names = {
        "manage_event",
        "manage_out_of_office",
        "manage_focus_time",
        "query_freebusy",
        "create_calendar",
    }

    # check if tools exist in mcp
    for name in calendar_tool_names:
        if not any(getattr(t, "name", "") == name for t in mcp_tools):
            print(f"Warning: Expected tool '{name}' not found in MCP tools. Make sure it is registered and available.")

    mcp_calendar_tools = [t for t in mcp_tools if getattr(t, "name", "") in calendar_tool_names]
    cli_tools = [cli_today_events, cli_list_events, cli_list_calendars, cli_get_event, cli_tool_list]
    all_tools = [*mcp_calendar_tools, *cli_tools]
    print("Calendar agent will use the following tools:")
    for tool in all_tools:
        print(f"  - {tool.name}")
    print(f"Using {len(all_tools)} tools for calendar agent.")
    llm = llm.bind_tools(all_tools)

    # Format system prompt with user email
    system_prompt = CALENDAR_SYSTEM_PROMPT.format(
        user_email=user_email,
        current_time=current_time
    )
    print(f"\nUsing system prompt with email: {user_email}\n")

    def agent_node(state: AgentState) -> AgentState:
        messages = [SystemMessage(content=system_prompt), *list(state["messages"])]
        response = llm.invoke(messages)
        if hasattr(response, 'tool_calls') and response.tool_calls:
            print("\n🔧 TOOL CALLS:")
            for tc in response.tool_calls:
                print(f"  → {tc['name']}")
                print(f"    Args: {json.dumps(tc['args'], indent=4)}")
            print()
        return {"messages": [response]}

    def should_continue(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(all_tools))
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


async def run_auto_refund_processing(agent) -> None:
    result = await agent.ainvoke(
        {
            "messages": [
                HumanMessage(
                    content=(
                        "Process all unread refund, return, and complaint emails in the inbox. "
                        "Classify each one, reply only when appropriate, and return a final summary."
                    )
                )
            ]
        }
    )
    final_message = result["messages"][-1]
    content = getattr(final_message, "content", final_message)
    
    # Render as markdown
    md = Markdown(content)
    console.print(md)

# async def run_auto_refund_processing(agent) -> None:
#     print("\n" + "="*60)
#     print("STARTING REFUND PROCESSING")
#     print("="*60)
    
#     # Step 1: Find all emails first
#     result = await agent.ainvoke(
#         {
#             "messages": [
#                 HumanMessage(
#                     content=(
#                         "First, search for ALL unread emails about refund, return, or complaint. "
#                         "Use search_gmail_messages and show me:\n"
#                         "- How many emails were found\n"
#                         "- The message_id and subject of EACH email\n"
#                         "- The sender (From field) of EACH email\n\n"
#                         "List ALL of them. Do not process them yet, just show me what you found."
#                     )
#                 )
#             ]
#         }
#     )
    
#     print("\n📧 EMAILS FOUND:")
#     found = result["messages"][-1]
#     print(getattr(found, 'content', found))
    
#     # Step 2: Now process each one explicitly
#     result = await agent.ainvoke(
#         {
#             "messages": [
#                 HumanMessage(
#                     content=(
#                         "Now I want you to process ALL of these emails. "
#                         "Read each one using get_gmail_message_content, "
#                         "classify it, and send appropriate replies using send_gmail_message. "
#                         "Make sure to:\n"
#                         "- Process EVERY email listed above\n"
#                         "- Use the correct sender email for the 'to' field\n"
#                         "- Use the thread_id from each message\n"
#                         "- After sending ALL replies, provide a summary of what was done\n\n"
#                         "Do NOT stop until you have processed ALL emails."
#                     )
#                 )
#             ]
#         }
#     )
    
#     final_message = result["messages"][-1]
#     print("\n" + "="*60)
#     print("PROCESSING COMPLETE:")
#     print("="*60)
#     print(getattr(final_message, 'content', final_message))


async def run_demo(agent) -> None:
    queries = [
        "What calendars do I have?",
        "What's on my calendar today?",
        "Show me my events for the next 7 days.",
    ]
    for i, query in enumerate(queries, start=1):
        print(f"\nDemo {i}: {query}")
        result = await agent.ainvoke({"messages": [HumanMessage(content=query)]})
        final_message = result["messages"][-1]
        content = getattr(final_message, "content", final_message)
        
        # Render as markdown
        md = Markdown(content)
        console.print(md)


async def run_interactive_chat(agent) -> None:
    history: list[BaseMessage] = []
    while True:
        user_text = input("You: ").strip()
        if user_text.lower() in {"exit", "quit"}:
            break
        if not user_text:
            continue
        history.append(HumanMessage(content=user_text))
        result = await agent.ainvoke({"messages": history})
        assistant_message = result["messages"][-1]
        history.append(assistant_message)
        content = getattr(assistant_message, 'content', assistant_message)
        
        # Render as markdown
        md = Markdown(content)
        console.print(f"Agent: ", end="")
        console.print(md)


async def main() -> None:
    parser = argparse.ArgumentParser(description="AI Workspace Agent Suite")
    parser.add_argument("agent", choices=["refund", "calendar"], help="Which agent to run")
    parser.add_argument(
        "--mode",
        choices=["auto", "interactive", "demo"],
        default="interactive",
        help="Run mode",
    )
    parser.add_argument(
        "--email",
        type=str,
        required=True,
        help="Google email address to use for calendar/email operations",
    )
    args = parser.parse_args()

    missing = _require_env(["OPENAI_API_KEY", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"])
    if missing:
        _print_setup_guide()
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")

    print(f"\nUsing Google email: {args.email}\n")

    start_mcp_server(MCP_SERVER_PORT)


    # print("Authenticating workspace-cli...")
    # subprocess.run(
    #     [
    #         "workspace-cli",
    #         "--url", f"http://localhost:{MCP_SERVER_PORT}/mcp",
    #         "call", 
    #         "list_calendars", 
    #         f"user_google_email={args.email}"
    #     ],
    #     timeout=60,
    #     check=False,
    # )
    # print("✓ Auth complete")
    mcp_client = MultiServerMCPClient(MCP_BASE_CONFIG)
    
    if args.agent == "refund":
        agent = await _build_refund_agent(mcp_client, args.email)
        if args.mode == "auto":
            await run_auto_refund_processing(agent)
        elif args.mode == "interactive":
            await run_interactive_chat(agent)
        else:
            print("Refund agent does not have a demo mode. Starting interactive chat instead.")
            await run_interactive_chat(agent)
        return

    agent = await _build_calendar_agent(mcp_client, args.email)
    if args.mode == "demo":
        await run_demo(agent)
    elif args.mode == "interactive":
        await run_interactive_chat(agent)
    else:
        print("Calendar agent does not have an auto mode. Starting interactive chat instead.")
        await run_interactive_chat(agent)


if __name__ == "__main__":
    asyncio.run(main())