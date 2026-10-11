"""Examiner-only: does LocalCLIAdapter deliver an instruction to the agent, and does MainAI get the agent's report back?
Uses /bin/echo as a harmless stand-in executable. Usage: python probe_cli_adapter_io.py <target-backend-dir>"""
import asyncio
import inspect
import sys
import tempfile
import uuid

sys.path.insert(0, sys.argv[1])
from app.agent_coordination.adapters import LocalCLIAdapter  # noqa: E402


async def main():
    a = LocalCLIAdapter(provider_key="probe", executable="/bin/echo", args_template=("STATIC-TEMPLATE",), cwd=tempfile.mkdtemp(), timeout_seconds=5, env={})
    aid = uuid.uuid4()
    params = list(inspect.signature(a.start_assignment).parameters)
    print(f"MATRIX|send:start_assignment accepts an instruction|{'HOLDS' if len(params) > 1 else 'GAP'}|parameters={params}")
    try:
        await a.send_instruction(aid, "fix the failing test")
        print("MATRIX|send:send_instruction|HOLDS|accepted")
    except NotImplementedError as e:
        print(f"MATRIX|send:send_instruction|GAP|NotImplementedError: {str(e)[:80]}")
    await a.start_assignment(aid)
    r = await a.collect_result(aid)
    has_text = "STATIC-TEMPLATE" in (r.summary + str(r.output_refs))
    print(f"MATRIX|receive:agent stdout content returned|{'HOLDS' if has_text else 'GAP'}|summary={r.summary!r} output_refs={r.output_refs}")


asyncio.run(main())
