"""Tool implementations for the Alex agent."""

from alex.tools.cron import TOOL_HINT as CRON_HINT, create_cron_tool
from alex.tools.executor import ToolExecutor
from alex.tools.fs import (
    TOOL_HINT_EDIT,
    TOOL_HINT_READ,
    TOOL_HINT_WRITE,
    FileReadTracker,
    create_edit_tool,
    create_read_tool,
    create_write_tool,
)
from alex.tools.git import TOOL_HINT as GIT_HINT, create_git_inspect_tool
from alex.tools.permissions import (
    DEFAULT_ALLOWED,
    KNOWN_PERMISSIONS,
    PERMISSION_DANGER,
    PERMISSION_NETWORK,
    PERMISSION_READ,
    PERMISSION_SHELL,
    PERMISSION_WRITE,
    AuditEvent,
    AuditLogger,
    PermissionPolicy,
    PreviewBlock,
    ToolApprovalRequest,
    attach_approval_summariser,
    build_approval_request,
    gate_tool_with_policy,
    gate_tools_with_policy,
    is_gated,
    required_permission,
)
from alex.tools.plugin_loader import (
    DEFAULT_PLUGIN_ROOT,
    PluginLoadResult,
    discover_plugin_files,
    install_plugins,
    load_plugins,
)
from alex.tools.ports import CronScheduler, ToolExecutionContext
from alex.tools.registry import ToolRegistry
from alex.tools.search import (
    TOOL_HINT_GLOB,
    TOOL_HINT_GREP,
    create_glob_tool,
    create_grep_tool,
)
from alex.tools.shell import (
    TOOL_HINT_BASH,
    TOOL_HINT_PWSH,
    create_available_shell_tools,
    create_bash_tool,
    create_pwsh_tool,
    detect_available_shells,
)
from alex.tools.time import TOOL_HINT as TIME_HINT, TimeInput, create_time_tool
from alex.tools.web_fetch import (
    TOOL_HINT as WEB_FETCH_HINT,
    WebFetchInput,
    create_web_fetch_tool,
)
from alex.tools.web_search import (
    TOOL_HINT as WEB_SEARCH_HINT,
    WebSearchInput,
    create_web_search_tool,
)

# Hints exposed in the system prompt — order is the recommended preference.
TOOL_HINTS = [
    TIME_HINT,
    WEB_SEARCH_HINT,
    WEB_FETCH_HINT,
    CRON_HINT,
    TOOL_HINT_READ,
    TOOL_HINT_WRITE,
    TOOL_HINT_EDIT,
    TOOL_HINT_GLOB,
    TOOL_HINT_GREP,
    GIT_HINT,
    TOOL_HINT_BASH,
    TOOL_HINT_PWSH,
]


def get_tool_hints() -> str:
    """Collect usage hints from all registered tool modules."""
    return "\n".join(f"- {h}" for h in TOOL_HINTS)


__all__ = [
    # Built-in tool factories
    "create_available_shell_tools",
    "create_bash_tool",
    "create_cron_tool",
    "create_edit_tool",
    "create_read_tool",
    "create_write_tool",
    "create_git_inspect_tool",
    "create_glob_tool",
    "create_grep_tool",
    "create_pwsh_tool",
    "create_time_tool",
    "create_web_fetch_tool",
    "create_web_search_tool",
    "detect_available_shells",
    # Tool input schemas (re-exported for tests / callers)
    "TimeInput",
    "WebFetchInput",
    "WebSearchInput",
    "FileReadTracker",
    # Hints
    "TOOL_HINTS",
    "get_tool_hints",
    # Runtime
    "CronScheduler",
    "ToolExecutionContext",
    "ToolExecutor",
    "ToolRegistry",
    # Permissions
    "DEFAULT_ALLOWED",
    "KNOWN_PERMISSIONS",
    "PERMISSION_DANGER",
    "PERMISSION_NETWORK",
    "PERMISSION_READ",
    "PERMISSION_SHELL",
    "PERMISSION_WRITE",
    "AuditEvent",
    "AuditLogger",
    "PermissionPolicy",
    "PreviewBlock",
    "ToolApprovalRequest",
    "attach_approval_summariser",
    "build_approval_request",
    "gate_tool_with_policy",
    "gate_tools_with_policy",
    "is_gated",
    "required_permission",
    # Plugin system
    "DEFAULT_PLUGIN_ROOT",
    "PluginLoadResult",
    "discover_plugin_files",
    "install_plugins",
    "load_plugins",
]
