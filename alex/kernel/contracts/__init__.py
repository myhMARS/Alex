"""Message contracts — every cross-module message type lives here.

Organised by domain.  Each contract is a plain dataclass inheriting from
``Event``, ``Command``, or ``Request`` (defined in ``alex.kernel.bus``).

Contracts are the ONLY cross-module types — no business module may import
another business module's internal types.
"""

# Chat contracts (UI events, commands)
from alex.kernel.contracts.chat import (
    FeedbackSubmitted,
    ThinkingUpdated,
    TokenEmitted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    UserTurnRequested,
)

# Tool contracts (catalog, execution, approval)
from alex.kernel.contracts.tools import (
    ExecuteTool,
    GetToolCatalog,
    InvokeProviderTool,
    RegisterTool,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolFinished,
    ToolsProvided,
    ToolStarted,
    UnregisterTool,
)

# Skill contracts (retrieval, loading, reflection)
from alex.kernel.contracts.skills import (
    DeleteSkill,
    DeprecateSkill,
    GetSkillName,
    ListSkills,
    LoadSkill,
    MergeSkills,
    RecordSkillUsage,
    ReflectSkills,
    RetrieveSkills,
    SkillLoaded,
    SkillReflectError,
    SkillsReflected,
)

# Memory contracts (context retrieval, mutation — all Request types)
from alex.kernel.contracts.memory import (
    AppendMessages,
    ClearMemory,
    GetContext,
    ReplaceMemory,
)

# Session contracts
from alex.kernel.contracts.session import (
    ListSessions,
    LoadSession,
    SaveSession,
    SessionRestored,
)

# Cron contracts
from alex.kernel.contracts.cron import (
    CancelCron,
    CronJobEvent,
    CronTurnRequested,
    ScheduleCron,
)

__all__ = [
    # Chat
    "FeedbackSubmitted",
    "ThinkingUpdated",
    "TokenEmitted",
    "TurnCompleted",
    "TurnFailed",
    "TurnStarted",
    "UserTurnRequested",
    # Tools
    "ExecuteTool",
    "GetToolCatalog",
    "InvokeProviderTool",
    "RegisterTool",
    "ToolApprovalRequested",
    "ToolApprovalResolved",
    "ToolFinished",
    "ToolsProvided",
    "ToolStarted",
    "UnregisterTool",
    # Skills
    "DeleteSkill",
    "DeprecateSkill",
    "GetSkillName",
    "ListSkills",
    "LoadSkill",
    "MergeSkills",
    "RecordSkillUsage",
    "ReflectSkills",
    "RetrieveSkills",
    "SkillLoaded",
    "SkillReflectError",
    "SkillsReflected",
    # Memory
    "AppendMessages",
    "ClearMemory",
    "GetContext",
    "ReplaceMemory",
    # Session
    "ListSessions",
    "LoadSession",
    "SaveSession",
    "SessionRestored",
    # Cron
    "CancelCron",
    "CronJobEvent",
    "CronTurnRequested",
    "ScheduleCron",
]
