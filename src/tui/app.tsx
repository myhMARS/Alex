/** OpenTUI React chat UI bound directly to the in-process TuiModule. */

import { existsSync } from 'node:fs';
import { rm } from 'node:fs/promises';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useKeyboard, useRenderer, useTerminalDimensions } from '@opentui/react';
import { RGBA } from '@opentui/core';
import type { InputRenderable, PasteEvent } from '@opentui/core';

import type { AttachmentDTO } from '../kernel/dto/message';
import { attachmentLabel, loadAttachment } from '../attachments/loader';
import { copyTextToClipboard, readClipboardImagePath } from '../attachments/clipboard';
import type {
  TuiModule,
  TuiPageKind,
  TuiSettingField,
  TuiSnapshot,
} from '../modules/tui/module';
import type {
  ApprovalView,
  CompactionPart,
  ToolCallView,
  TurnPart,
  TurnView,
} from '../modules/tui/view-state';
import { formatMarkdown } from './markdown';
import { MarkdownText } from './markdown-view';
import { theme } from './theme';
import { formatToolOutput, summarizeToolArgs } from './tool-format';
import { formatApprovalPreview } from './approval-format';

const PANEL_BG = RGBA.fromInts(24, 24, 37, 220);
const ACTIVE_ROW_BG = RGBA.fromInts(49, 50, 68, 235);

interface PaletteCommand {
  name: string;
  description: string;
  run: () => void;
}

const PALETTE_MAX_VISIBLE = 10;
const SIDEBAR_WIDTH = 30;
const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

const PAGE_COMMANDS: Record<string, TuiPageKind> = {
  '/help': 'help',
  '/h': 'help',
  '/skills': 'skills',
  '/skill': 'skills',
  '/cron': 'cron',
  '/mcp': 'mcp',
  '/sessions': 'sessions',
  '/resume': 'sessions',
  '/reflections': 'reflections',
  '/reflect': 'reflections',
  '/context': 'context',
  '/ctx': 'context',
  '/settings': 'settings',
  '/config': 'settings',
  '/log': 'log',
  '/prompt': 'prompt',
  '/agents': 'agents',
  '/mailbox': 'mailbox',
  '/lineage': 'lineage',
  '/tasks': 'tasks',
  '/teams': 'teams',
};

export interface TuiAppProps {
  tui: TuiModule;
  sessionId: string;
  toolCount?: number;
  onQuit: () => void;
}

export function TuiApp({ tui, sessionId, toolCount, onQuit }: TuiAppProps) {
  const snapshot = React.useSyncExternalStore(tui.subscribe, tui.getSnapshot, tui.getSnapshot);
  const { width, height } = useTerminalDimensions();
  const renderer = useRenderer();
  const toggleProcessTurn = useCallback(
    (turnId: string) => {
      // A disclosure click must not start or extend text selection over the
      // rows that are about to move.
      renderer.clearSelection();
      tui.toggleToolTurn(turnId);
    },
    [renderer, tui],
  );
  const inputRef = useRef<InputRenderable | null>(null);
  const approval = snapshot.approvals[0] ?? null;
  const page = snapshot.page;
  const activeSessionId = snapshot.sessionId || sessionId;
  const showSidebar = width >= 90;
  const approvalPreview = approval
    ? formatApprovalPreview(approval.preview, Math.max(24, width - 8))
    : null;
  const approvalVisibleLines = Math.max(4, height - 12);
  const [approvalOffset, setApprovalOffset] = useState(0);
  const [inputValue, setInputValue] = useState('');
  const [pendingAttachments, setPendingAttachments] = useState<AttachmentDTO[]>([]);
  const [paletteIndex, setPaletteIndex] = useState(0);
  const [paletteDismissed, setPaletteDismissed] = useState(false);
  const [settingsIndex, setSettingsIndex] = useState(0);
  const [inlineEdit, setInlineEdit] = useState<TuiSettingField | null>(null);
  const [inlineDraft, setInlineDraft] = useState('');
  const [pendingTeamDispose, setPendingTeamDispose] = useState<string | null>(null);
  const approvalMaxOffset = approvalPreview
    ? Math.max(0, approvalPreview.lines.length - approvalVisibleLines)
    : 0;

  useEffect(() => {
    setApprovalOffset(0);
  }, [approval?.reqId]);

  useEffect(() => {
    setPaletteIndex(0);
    setPaletteDismissed(false);
  }, [inputValue]);

  useEffect(() => {
    setSettingsIndex(0);
    setInlineEdit(null);
    setInlineDraft('');
  }, [page?.kind]);

  // The module owns the active session; seed it from the launcher/tests.
  useEffect(() => {
    if (!tui.getSnapshot().sessionId && sessionId) tui.setSessionId(sessionId);
  }, [tui, sessionId]);

  const paletteCommands = useMemo<PaletteCommand[]>(
    () => [
      { name: '/help', description: '显示命令帮助', run: () => void tui.showPage('help') },
      { name: '/skills', description: '已学习技能列表', run: () => void tui.showPage('skills') },
      {
        name: '/reflect',
        description: '立即触发一次技能反思',
        run: () => {
          const result = tui.reflectSkills();
          tui.setToast(result.message);
        },
      },
      {
        name: '/merge',
        description: '合并语义重复的 skills',
        run: () => void tui.mergeSkills().then((result) => tui.setToast(result.message)),
      },
      { name: '/cron', description: '定时任务列表', run: () => void tui.showPage('cron') },
      { name: '/mcp', description: 'MCP 服务器状态', run: () => void tui.showPage('mcp') },
      {
        name: '/sessions',
        description: '会话列表 / 恢复历史会话',
        run: () => void tui.showPage('sessions'),
      },
      {
        name: '/settings',
        description: '模型设置（API Key / 上下文 / 输出 token）',
        run: () => void tui.showPage('settings'),
      },
      {
        name: '/context',
        description: '当前对话上下文窗口状态',
        run: () => void tui.showPage('context'),
      },
      {
        name: '/teams',
        description: '查看所有 Team 汇总',
        run: () => void tui.showPage('teams'),
      },
      {
        name: '/tasks',
        description: '查看 Agent task 详情（当前 session）',
        run: () => void tui.showPage('tasks'),
      },
      {
        name: '/task',
        description: '查看单个 task 与关联 session 事件',
        run: () => tui.setToast('用法：/task <task_id>'),
      },
      {
        name: '/team',
        description: '按 team 过滤 Agent / Task / Mailbox',
        run: () => tui.setToast('用法：/team <team_id>（空=全部）'),
      },
      {
        name: '/dispose',
        description: 'dispose 一个 worker agent',
        run: () => tui.setToast('用法：/dispose <agent_id>'),
      },
      {
        name: '/set',
        description: '修改设置，如 /set max_iterations 20',
        run: () => tui.setToast('用法：/set <key> <value>'),
      },
      {
        name: '/resume',
        description: '恢复历史主会话',
        run: () => void tui.showPage('sessions'),
      },
      {
        name: '/lineage',
        description: '查看 Agent / Session 谱系树',
        run: () => void tui.showPage('lineage'),
      },
      {
        name: '/mailbox',
        description: '查看 Agent mailbox',
        run: () => void tui.showPage('mailbox'),
      },
      {
        name: '/agents',
        description: '查看 Agent Team 与任务状态',
        run: () => void tui.showPage('agents'),
      },
      {
        name: '/prompt',
        description: '查看 system prompt 组装结果',
        run: () => void tui.showPage('prompt'),
      },
      {
        name: '/reload-prompts',
        description: '重新读取用户 prompt / AGENTS.md',
        run: () => {
          void tui.reloadPrompts().then((result) => tui.setToast(result.message));
        },
      },
      {
        name: '/log',
        description: '当前会话事件日志（含压缩前原始事件）',
        run: () => void tui.showPage('log'),
      },
      {
        name: '/checkpoint',
        description: '将当前会话 JSONL 刷盘',
        run: () => {
          void tui.checkpointSession().then((result) => tui.setToast(result.message));
        },
      },
      {
        name: '/repair',
        description: '修复当前会话 JSONL 的损坏尾部',
        run: () => {
          void tui.repairSession().then((result) => tui.setToast(result.message));
        },
      },
      {
        name: '/fork',
        description: '从当前会话复制一个新会话',
        run: () => {
          void tui.forkSession();
        },
      },
      {
        name: '/compact',
        description: '手动压缩上下文（较早历史摘要）',
        run: () => {
          void tui
            .compactContext()
            .then((result) =>
              tui.setToast(
                result.skipped
                  ? '无需压缩（历史太短或没有可安全压缩的内容）'
                  : `✓ 已压缩 ${result.compactedMessages} 条 · 释放 ~${result.freedTokens.toLocaleString()} tokens`,
              ),
            )
            .catch((error: unknown) =>
              tui.setToast(`✗ 压缩失败：${error instanceof Error ? error.message : String(error)}`),
            );
        },
      },
      {
        name: '/reflections',
        description: '最近的技能反思记录',
        run: () => void tui.showPage('reflections'),
      },
      {
        name: '/image',
        description: '发送图片：/image <path>',
        run: () => tui.setToast('用法：/image <path>'),
      },
      {
        name: '/file',
        description: '发送文件：/file <path>',
        run: () => tui.setToast('用法：/file <path>'),
      },
      {
        name: '/effort',
        description: '模型强度：nothinking / low / high / max',
        run: () => {
          void tui.setEffort('').then((result) => tui.setToast(result.message));
        },
      },
      { name: '/new', description: '新建会话', run: () => void tui.newSession() },
      { name: '/quit', description: '退出 Alex', run: onQuit },
    ],
    [tui, onQuit],
  );

  const paletteQuery =
    inputValue.startsWith('/') && !inputValue.includes(' ')
      ? inputValue.slice(1).toLowerCase()
      : null;
  const paletteMatches =
    paletteQuery === null
      ? []
      : paletteCommands.filter((command) =>
          command.name.slice(1).toLowerCase().startsWith(paletteQuery),
        );
  const paletteVisible =
    !approval && paletteQuery !== null && paletteMatches.length > 0 && !paletteDismissed;
  const paletteSelected = Math.min(
    paletteIndex,
    Math.max(0, paletteMatches.length - 1),
  );
  const paletteStart = Math.max(
    0,
    Math.min(
      paletteSelected - PALETTE_MAX_VISIBLE + 1,
      paletteMatches.length - PALETTE_MAX_VISIBLE,
    ),
  );
  const paletteItems = paletteMatches.slice(
    paletteStart,
    paletteStart + PALETTE_MAX_VISIBLE,
  );

  const clearInput = useCallback(() => {
    if (inputRef.current) inputRef.current.value = '';
    setInputValue('');
    setPaletteIndex(0);
    setPaletteDismissed(false);
  }, []);

  const beginInlineEdit = useCallback(
    (field: TuiSettingField, index: number) => {
      renderer.clearSelection();
      setSettingsIndex(index);
      setInlineEdit(field);
      setInlineDraft('');
    },
    [renderer],
  );

  const activateSettingField = useCallback(
    (field: TuiSettingField, index: number) => {
      renderer.clearSelection();
      setSettingsIndex(index);
      let next: string | null = null;
      if (field.kind === 'toggle') {
        next = field.value === 'on' ? 'off' : 'on';
      } else if (field.kind === 'choice' && field.choices?.length) {
        const current = field.choices.indexOf(field.value);
        next = field.choices[(current + 1) % field.choices.length] ?? null;
      }
      if (next === null) {
        beginInlineEdit(field, index);
        return;
      }
      void tui.updateSetting(field.key, next).then((result) => {
        tui.setToast(`${result.ok ? '✓' : '✗'} ${result.message}`);
      });
    },
    [renderer, tui, beginInlineEdit],
  );

  useEffect(() => {
    if (snapshot.page?.kind !== 'teams') setPendingTeamDispose(null);
  }, [snapshot.page?.kind]);

  useKeyboard((key) => {
    if (key.ctrl && key.name === 'c') {
      const selected = renderer.getSelection()?.getSelectedText() ?? '';
      if (selected.trim()) {
        renderer.clearSelection();
        void copyTextToClipboard(selected).then((ok) => {
          tui.setToast(ok ? `已复制 ${selected.length} 字符` : '✗ 复制失败：没有可用的剪贴板命令');
        });
        return;
      }
      onQuit();
      return;
    }
    if (key.ctrl && key.name === 't') {
      const expanded = tui.toggleThinking();
      tui.setToast(expanded ? 'Thinking 已展开' : 'Thinking 已收起');
      return;
    }
    if (key.ctrl && key.name === 'k') {
      const expanded = tui.toggleSkills();
      tui.setToast(expanded ? 'Skills 已展开' : 'Skills 已收起');
      return;
    }
    if (key.ctrl && key.name === 'o') {
      const expanded = tui.toggleToolOutput();
      tui.setToast(expanded ? '工具输出已展开' : '工具输出已收起');
      return;
    }
    if (key.name === 'f5') {
      if (snapshot.sidebarCollapsed) tui.toggleSidebar();
      void tui.refreshFileTree();
      tui.setToast('文件树已刷新');
      return;
    }
    if (
      key.name === 'f1' ||
      key.name === 'f2' ||
      key.name === 'f3' ||
      (key.ctrl && (key.name === '1' || key.name === '2' || key.name === '3'))
    ) {
      if (snapshot.sidebarCollapsed) tui.toggleSidebar();
      const wantsFiles = key.name === 'f2' || key.name === '2';
      const wantsAgents = key.name === 'f3' || key.name === '3';
      void tui.setSidebarTab(wantsAgents ? 'agents' : wantsFiles ? 'files' : 'overview');
      return;
    }
    if (key.ctrl && key.name === 'tab') {
      if (snapshot.sidebarCollapsed) tui.toggleSidebar();
      const next =
        snapshot.sidebarTab === 'overview'
          ? 'files'
          : snapshot.sidebarTab === 'files'
            ? 'agents'
            : 'overview';
      void tui.setSidebarTab(next);
      return;
    }
    if (key.ctrl && key.name === 's') {
      const collapsed = tui.toggleSidebar();
      tui.setToast(collapsed ? '侧栏已收起' : '侧栏已展开');
      return;
    }
    if (key.shift && key.name === 'tab') {
      void tui.cyclePermissionMode().then((mode) => {
        tui.setToast(`权限模式：${permissionModeLabel(mode)}`);
      });
      return;
    }
    if (key.ctrl && key.name === 'g') {
      tui.submitFeedback(true);
      return;
    }
    if (key.ctrl && key.name === 'b') {
      tui.submitFeedback(false);
      return;
    }
    if (paletteVisible) {
      if (key.name === 'up') {
        setPaletteIndex((value) => Math.max(0, value - 1));
        return;
      }
      if (key.name === 'down') {
        setPaletteIndex((value) => Math.min(paletteMatches.length - 1, value + 1));
        return;
      }
      if (key.name === 'escape') {
        setPaletteDismissed(true);
        return;
      }
      if (key.name === 'f6') {
        void attachClipboardImage();
        return;
      }
    }
    if (key.name === 'f6') {
      void attachClipboardImage();
      return;
    }
    if (key.name === 'escape' && pendingAttachments.length > 0) {
      setPendingAttachments([]);
      tui.setToast('已清空待发送附件');
      return;
    }
    if (inlineEdit && key.name === 'escape') {
      setInlineEdit(null);
      setInlineDraft('');
      return;
    }
    if (
      page?.kind === 'settings' &&
      !inlineEdit &&
      !approval &&
      !paletteVisible &&
      !inputValue &&
      (page.fields?.length ?? 0) > 0
    ) {
      const count = page.fields?.length ?? 0;
      if (key.name === 'up') {
        setSettingsIndex((value) => Math.max(0, value - 1));
        return;
      }
      if (key.name === 'down') {
        setSettingsIndex((value) => Math.min(count - 1, value + 1));
        return;
      }
      if (key.name === 'left' || key.name === 'right') {
        const fields = page.fields ?? [];
        const field = fields[Math.min(settingsIndex, fields.length - 1)];
        if (field?.kind === 'toggle' || field?.kind === 'choice') {
          activateSettingField(field, settingsIndex);
          return;
        }
      }
    }
    if (key.name === 'escape' && page) {
      tui.dismissPage();
      return;
    }
    const fileMode =
      !approval &&
      sidebarVisible &&
      snapshot.sidebarTab === 'files' &&
      !inputValue &&
      !paletteVisible;
    if (fileMode) {
      if (key.name === 'up') {
        tui.moveFileSelection(-1);
        return;
      }
      if (key.name === 'down') {
        tui.moveFileSelection(1);
        return;
      }
      if (key.name === 'right') {
        void tui.expandSelectedDir();
        return;
      }
      if (key.name === 'left') {
        tui.collapseSelectedDir();
        return;
      }
      if (key.name === 'return' || key.name === 'enter') {
        void tui.toggleSelectedDir();
        return;
      }
    }
    if (!approval) return;
    if (key.name === 'up') {
      setApprovalOffset((value) => Math.max(0, value - 1));
      return;
    }
    if (key.name === 'down') {
      setApprovalOffset((value) => Math.min(approvalMaxOffset, value + 1));
      return;
    }
    if (key.name === 'pageup') {
      setApprovalOffset((value) => Math.max(0, value - approvalVisibleLines));
      return;
    }
    if (key.name === 'pagedown') {
      setApprovalOffset((value) =>
        Math.min(approvalMaxOffset, value + approvalVisibleLines),
      );
      return;
    }
    if (key.name === 'home') {
      setApprovalOffset(0);
      return;
    }
    if (key.name === 'end') {
      setApprovalOffset(approvalMaxOffset);
      return;
    }
    if (key.name === 'y' || key.name === 'a') {
      tui.resolveApproval(approval.reqId, true, false);
    } else if (key.name === 's') {
      tui.resolveApproval(approval.reqId, true, true);
    } else if (key.name === 'n' || key.name === 'escape') {
      tui.resolveApproval(approval.reqId, false, false);
    }
  });

  // The input is uncontrolled: typing never re-renders the whole app.
  const handlePaste = useCallback(
    async (event: PasteEvent) => {
      const bytes = event.bytes;
      const mime = event.metadata?.mimeType ?? '';
      if (event.metadata?.kind === 'binary' && mime.startsWith('image/')) {
        event.preventDefault();
        const dataUrl = `data:${mime};base64,${Buffer.from(bytes).toString('base64')}`;
        setPendingAttachments((current) => [
          ...current,
          { kind: 'image', name: 'pasted-image', size: bytes.length, dataUrl },
        ]);
        tui.setToast('已附加剪贴板图片');
        return;
      }
      const text = new TextDecoder().decode(bytes).trim();
      if (!text || text.includes('\0')) return;
      const candidates = text
        .split(/\r?\n/)
        .map((line) => line.trim().replace(/^["']|["']$/g, ''))
        .filter(Boolean);
      if (candidates.length === 0 || candidates.length > 10) return;
      if (!candidates.every((path) => existsSync(path))) return;
      event.preventDefault();
      try {
        const attachments = await Promise.all(
          candidates.map((path) => loadAttachment(path)),
        );
        setPendingAttachments((current) => [...current, ...attachments]);
        tui.setToast(`已附加 ${attachments.length} 个文件，Enter 发送`);
      } catch (error) {
        tui.setToast(`✗ 附件失败：${error instanceof Error ? error.message : String(error)}`);
      }
    },
    [tui],
  );

  const attachClipboardImage = useCallback(async () => {
    try {
      const path = await readClipboardImagePath();
      if (!path) {
        tui.setToast('剪贴板里没有图片（Windows 需复制图片内容而非文件路径）');
        return;
      }
      try {
        const attachment = await loadAttachment(path, 'image');
        setPendingAttachments((current) => [...current, attachment]);
        tui.setToast(`已附加剪贴板图片 ${attachment.name}，Enter 发送`);
      } finally {
        await rm(path, { force: true }).catch(() => undefined);
      }
    } catch (error) {
      tui.setToast(`✗ 读取剪贴板失败：${error instanceof Error ? error.message : String(error)}`);
    }
  }, [tui]);

  const handleSubmit = useCallback(
    async (raw: unknown) => {
      const rawText = typeof raw === 'string' ? raw : '';
      // Normalize full-width characters (Chinese IME: ：／……) so `:q` still works.
      const text = rawText
        .replace(/[\uFF01-\uFF5E]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xfee0))
        .replace(/\u3000/g, ' ')
        .trim();
      // Read the live snapshot: the callback closes over an older `page` value.
      const currentPage = tui.getSnapshot().page;
      if (
        !text &&
        !inlineEdit &&
        currentPage?.kind === 'settings' &&
        (currentPage.fields?.length ?? 0) > 0
      ) {
        const fields = currentPage.fields ?? [];
        const field = fields[Math.min(settingsIndex, fields.length - 1)];
        if (field) activateSettingField(field, settingsIndex);
        return;
      }
      if (!text && pendingAttachments.length === 0) return;
      const lower = text.toLowerCase();
      if (paletteVisible) {
        const command = paletteMatches[paletteSelected] ?? paletteMatches[0];
        if (command) {
          clearInput();
          command.run();
          return;
        }
      }
      if (lower === '/quit' || lower === 'quit' || lower === 'exit') {
        onQuit();
        return;
      }
      if (currentPage && (text === ':q' || lower === '/close')) {
        tui.dismissPage();
        clearInput();
        return;
      }
      if (currentPage?.kind === 'sessions') {
        const index = Number.parseInt(text, 10);
        if (Number.isInteger(index) && index > 0) {
          void tui.chooseSession(index);
          clearInput();
          return;
        }
      }
      if (lower === '/mcp reload' || lower === '/mcp refresh') {
        clearInput();
        void tui.reloadMcp().then((result) => {
          tui.setToast(`${result.ok ? '✓' : '✗'} ${result.message}`);
        });
        return;
      }
      if (currentPage?.kind === 'mcp') {
        const action = text.match(/^([ed])\s*(\d+)$/i);
        if (action) {
          clearInput();
          void tui
            .setMcpServerEnabled(Number.parseInt(action[2]!, 10), action[1]!.toLowerCase() === 'e')
            .then((result) => {
              tui.setToast(`${result.ok ? '✓' : '✗'} ${result.message}`);
            });
          return;
        }
        const index = Number.parseInt(text, 10);
        if (Number.isInteger(index) && index > 0) {
          tui.toggleMcpServer(index);
          clearInput();
          return;
        }
      }
      const setMatch = text.match(/^\/set\s+(\S+)\s+(.+)$/i);
      if (setMatch) {
        const key = setMatch[1] ?? '';
        const value = setMatch[2] ?? '';
        clearInput();
        void tui.updateSetting(key, value).then((result) => {
          tui.setToast(`${result.ok ? '✓' : '✗'} ${result.message}`);
          if (result.ok) void tui.showPage('settings');
        });
        return;
      }

      const attachCommand = text.match(
        /^\/(image|file)\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s*([\s\S]*)$/i,
      );
      if (attachCommand) {
        const kind = attachCommand[1] === 'image' ? 'image' : 'file';
        const path = attachCommand[2] ?? attachCommand[3] ?? attachCommand[4] ?? '';
        const caption = (attachCommand[5] ?? '').trim();
        clearInput();
        try {
          const attachment = await loadAttachment(path, kind);
          tui.submitUserTurn(caption, undefined, [attachment]);
          tui.setToast(`已附加 ${kind === 'image' ? '图片' : '文件'}：${attachment.name}`);
        } catch (error) {
          tui.setToast(`✗ 附件失败：${error instanceof Error ? error.message : String(error)}`);
        }
        return;
      }

      const inlinePaths: string[] = [];
      const stripped = text
        .replace(/(^|\s)@(?:"([^"]+)"|'([^']+)'|(\S+))/g, (_match, lead, a, b, c) => {
          inlinePaths.push(String(a ?? b ?? c));
          return lead;
        })
        .trim();
      if (inlinePaths.length > 0) {
        clearInput();
        try {
          const attachments = await Promise.all(
            inlinePaths.map((path) => loadAttachment(path)),
          );
          tui.submitUserTurn(stripped, undefined, attachments);
        } catch (error) {
          tui.setToast(`✗ 附件失败：${error instanceof Error ? error.message : String(error)}`);
        }
        return;
      }

      if (lower.startsWith('/effort')) {
        const value = text.slice('/effort'.length).trim();
        clearInput();
        void tui.setEffort(value).then((result) => tui.setToast(result.message));
        return;
      }

      if (lower === '/task' || lower.startsWith('/task ')) {
        const taskId = text.slice('/task'.length).trim();
        clearInput();
        void tui.showTaskDetail(taskId);
        return;
      }

      if (lower === '/team' || lower.startsWith('/team ')) {
        const value = text.slice('/team'.length).trim();
        clearInput();
        tui.setTeamFilter(value);
        tui.setToast(value ? `Team 过滤：${value}` : 'Team 过滤：全部');
        return;
      }

      if (lower === '/reload-prompts') {
        clearInput();
        void tui.reloadPrompts().then((result) => tui.setToast(result.message));
        return;
      }

      if (lower === '/checkpoint') {
        clearInput();
        void tui.checkpointSession().then((result) => tui.setToast(result.message));
        return;
      }

      if (lower === '/repair') {
        clearInput();
        void tui.repairSession().then((result) => tui.setToast(result.message));
        return;
      }

      if (lower === '/fork') {
        clearInput();
        void tui.forkSession();
        return;
      }

      if (lower === '/compact') {
        clearInput();
        void tui
          .compactContext()
          .then((result) =>
            tui.setToast(
              result.skipped
                ? '无需压缩（历史太短或没有可安全压缩的内容）'
                : `✓ 已压缩 ${result.compactedMessages} 条 · 释放 ~${result.freedTokens.toLocaleString()} tokens`,
            ),
          )
          .catch((error: unknown) =>
            tui.setToast(`✗ 压缩失败：${error instanceof Error ? error.message : String(error)}`),
          );
        return;
      }

      if (lower === '/reflect') {
        clearInput();
        const result = tui.reflectSkills();
        tui.setToast(result.message);
        return;
      }
      if (lower === '/merge') {
        clearInput();
        void tui.mergeSkills().then((result) => tui.setToast(result.message));
        return;
      }

      const showAllTasks = lower === '/agents all' || lower === '/tasks all';
      if (showAllTasks) {
        tui.setTaskScope('all');
        void tui.showPage(lower.startsWith('/agents') ? 'agents' : 'tasks');
        clearInput();
        return;
      }

      const pageKind = PAGE_COMMANDS[lower];
      if (pageKind) {
        if (pageKind === 'agents' || pageKind === 'tasks') {
          tui.setTaskScope('session');
        }
        void tui.showPage(pageKind);
        clearInput();
        return;
      }
      if (lower === '/new') {
        clearInput();
        setPendingAttachments([]);
        void tui.newSession();
        return;
      }
      if (lower.startsWith('/dispose')) {
        clearInput();
        const agentId = text.slice('/dispose'.length).trim();
        if (!agentId) {
          tui.setToast('用法：/dispose <agent_id>');
          return;
        }
        void tui.disposeAgent(agentId).then((result) => tui.setToast(result.message));
        return;
      }
      if (lower.startsWith('/')) {
        void tui.showPage('help', `未知命令：${text}`);
        clearInput();
        return;
      }
      if (currentPage) tui.dismissPage();
      tui.submitUserTurn(text, undefined, pendingAttachments);
      clearInput();
      setPendingAttachments([]);
    },
    [
      tui,
      sessionId,
      onQuit,
      clearInput,
      paletteVisible,
      paletteMatches,
      paletteSelected,
    ],
  );

  const sidebarVisible =
    showSidebar && !snapshot.sidebarCollapsed && !approval && !page;
  const contentWidth = Math.max(40, width - 2 - (sidebarVisible ? SIDEBAR_WIDTH : 0));


  return (
    <box
      style={{
        flexDirection: 'column',
        width: '100%',
        height: '100%',
        overflow: 'hidden',
        position: 'relative',
      }}
    >
      <Header
        sessionId={activeSessionId}
        toolCount={toolCount}
        collapsed={snapshot.sidebarCollapsed}
        sidebarAvailable={showSidebar}
      />
      <box style={{ flexDirection: 'row', flexGrow: 1, overflow: 'hidden' }}>
        <box
          style={{
            flexDirection: 'column',
            flexGrow: 1,
            overflow: 'hidden',
            paddingLeft: 1,
            paddingRight: 1,
          }}
        >
          {approval ? (
            <ApprovalPanel
              approval={approval}
              preview={approvalPreview!}
              offset={approvalOffset}
              visibleLines={approvalVisibleLines}
              width={contentWidth}
            />
          ) : page ? (
            <PagePanel
              page={page}
              width={contentWidth}
              selectedIndex={settingsIndex}
              editingKey={inlineEdit?.key}
              draft={inlineDraft}
              onBeginEdit={beginInlineEdit}
              onActivateField={activateSettingField}
              onDraftChange={setInlineDraft}
              onMcpToggle={(index, enabled) => {
                void tui.setMcpServerEnabled(index, enabled).then((result) => {
                  tui.setToast(`${result.ok ? '✓' : '✗'} ${result.message}`);
                });
              }}
              pendingTeamId={pendingTeamDispose}
              onTeamDispose={(teamId) => {
                if (pendingTeamDispose === teamId) {
                  setPendingTeamDispose(null);
                  void tui.disposeTeam(teamId).then((result) => {
                    tui.setToast(`${result.ok ? '✓' : '✗'} ${result.message}`);
                  });
                } else {
                  setPendingTeamDispose(teamId);
                  tui.setToast(`再次点击 [确认解散] 以解散 Team：${teamId}`);
                }
              }}
              onSaveEdit={(value) => {
                const field = inlineEdit;
                setInlineEdit(null);
                setInlineDraft('');
                if (!field || !value.trim()) return;
                void tui.updateSetting(field.key, value).then((result) => {
                  tui.setToast(`${result.ok ? '✓' : '✗'} ${result.message}`);
                });
              }}
            />
          ) : snapshot.turns.length === 0 && snapshot.streaming.length === 0 ? (
            <Welcome width={contentWidth} />
          ) : (
            <scrollbox
              style={{ flexGrow: 1, width: '100%' }}
              stickyScroll
              stickyStart="bottom"
              scrollbarOptions={{ visible: false }}
            >
              <Chat
                turns={snapshot.turns}
                width={contentWidth}
                thinkingExpanded={snapshot.thinkingExpanded}
                skillsExpanded={snapshot.skillsExpanded}
                toolOutputExpanded={snapshot.toolOutputExpanded}
                toolTurnsExpanded={snapshot.toolTurnsExpanded}
                onToggleToolTurn={toggleProcessTurn}
              />
            </scrollbox>
          )}
        </box>
        {sidebarVisible ? (
          <Sidebar
            sidebar={snapshot.sidebar}
            sessionId={activeSessionId}
            reflection={snapshot.reflection}
            tab={snapshot.sidebarTab}
            agents={snapshot.sidebarAgents}
            viewAgentId={snapshot.viewAgentId}
            onAgentSelect={(agentId) => void tui.selectAgent(agentId)}
            onAgentDispose={(agentId) =>
              void tui.disposeAgent(agentId).then((result) => tui.setToast(result.message))
            }
            fileTree={snapshot.fileTree}
            fileRows={snapshot.fileRows}
            fileSelected={snapshot.fileSelected}
            onTabSelect={(next) => void tui.setSidebarTab(next)}
          />
        ) : null}
      </box>

      {paletteVisible ? (
        <CommandPalette
          items={paletteItems}
          start={paletteStart}
          total={paletteMatches.length}
          selected={paletteSelected}
        />
      ) : null}

      {pendingAttachments.length > 0 ? (
        <box style={{ height: 1, paddingLeft: 1, paddingRight: 1 }}>
          <text fg={theme.muted} style={{ width: '100%' }}>
            📎 {pendingAttachments.map((attachment) => attachmentLabel(attachment)).join(' · ')}
          </text>
        </box>
      ) : null}

      {snapshot.queued.length > 0 ? (
        <QueueDock
          messages={snapshot.queued}
          width={width}
          onCancel={(queueId) => {
            renderer.clearSelection();
            const result = tui.cancelQueuedMessage(queueId);
            tui.setToast(`${result.ok ? '✓' : '✗'} ${result.message}`);
          }}
        />
      ) : null}

      <box
        style={{
          height: 2,
          border: ['top'],
          borderColor: theme.border,
          flexDirection: 'row',
          paddingLeft: 1,
          paddingRight: 1,
        }}
      >
        <text fg={theme.accent}>❯ </text>
        <input
          ref={inputRef}
          style={{ flexGrow: 1 }}
          placeholder={
            approval
              ? 'y approve · s always · n deny'
              : page?.kind === 'settings'
                ? 'click a setting value to edit · Esc close'
                  : page
                    ? ':q close page · /help for commands'
                    : 'Message Alex… (/help for commands, /quit to exit)'
          }
          onInput={(value) => setInputValue(value)}
          onPaste={(event) => {
            void handlePaste(event);
          }}
          onSubmit={handleSubmit}
          focused={!approval && !inlineEdit}
        />
      </box>

      <StatusBar snapshot={snapshot} width={width} />

      {snapshot.toast ? (
        <box
          style={{
            position: 'absolute',
            bottom: 4,
            right: 2,
            border: true,
            borderStyle: 'rounded',
            borderColor: theme.accent,
            backgroundColor: 'transparent',
            paddingLeft: 1,
            paddingRight: 1,
            maxWidth: Math.max(24, width - 8),
            zIndex: 20,
          }}
        >
          <text fg={theme.text}>◆ {snapshot.toast}</text>
        </box>
      ) : null}
    </box>
  );
}

function QueueDock({
  messages,
  width,
  onCancel,
}: {
  messages: TuiSnapshot['queued'];
  width: number;
  onCancel: (queueId: string) => void;
}) {
  const visible = messages.slice(0, 3);
  return (
    <box
      style={{
        flexDirection: 'column',
        flexShrink: 0,
        height: 1 + visible.length + (messages.length > visible.length ? 1 : 0),
        paddingLeft: 1,
        paddingRight: 1,
        backgroundColor: PANEL_BG,
        width: '100%',
      }}
    >
      <text fg={theme.accent}>
        ↳ Queued ({messages.length}) · sends after the current turn · click ✕ to remove
      </text>
      {visible.map((message, index) => (
        <text
          key={message.queueId}
          fg={theme.muted}
          style={{ width: width - 2 }}
          selectable={false}
          onMouseDown={() => onCancel(message.queueId)}
        >
          {`  ${index + 1}. ${truncate(message.userInput.replace(/\s+/g, ' '), Math.max(8, width - 16))}`}
          {message.attachments.length > 0 ? `  (+${message.attachments.length} attachment(s))` : ''}
          <span fg={theme.error}>  ✕</span>
        </text>
      ))}
      {messages.length > visible.length ? (
        <text fg={theme.border}>{`  … ${messages.length - visible.length} more`}</text>
      ) : null}
    </box>
  );
}

function CommandPalette({
  items,
  start,
  total,
  selected,
}: {
  items: PaletteCommand[];
  start: number;
  total: number;
  selected: number;
}) {
  return (
    <box
      style={{
        flexDirection: 'column',
        width: '100%',
        backgroundColor: PANEL_BG,
        paddingLeft: 1,
        paddingRight: 1,
      }}
    >
      <box style={{ flexDirection: 'row' }}>
        <text fg={theme.accent}>
          <strong>Commands ({total})</strong>
        </text>
        <text fg={theme.muted}>
          {'  '}
          {items.length > 0 ? `${start + 1}-${start + items.length}/${total}` : '0/'}
          {' · ↑↓ 选择 · Enter 执行 · Esc 关闭'}
        </text>
      </box>
      {items.map((command, index) => {
        const globalIndex = start + index;
        const active = globalIndex === selected;
        return (
          <box
            key={command.name}
            style={{
              flexDirection: 'row',
              paddingLeft: 1,
              backgroundColor: active ? ACTIVE_ROW_BG : undefined,
            }}
          >
            <text fg={active ? theme.accent : theme.muted}>{active ? '❯ ' : '  '}</text>
            <text fg={active ? theme.text : theme.muted}>{command.name.padEnd(11, ' ')}</text>
            <text fg={active ? theme.text : theme.border}>{command.description}</text>
          </box>
        );
      })}
      {total > items.length ? (
        <text fg={theme.border}>  … {total - items.length} more · 继续 ↑↓ 查看</text>
      ) : null}
    </box>
  );
}

function Header({
  sessionId,
  toolCount,
  collapsed,
  sidebarAvailable,
}: {
  sessionId: string;
  toolCount?: number;
  collapsed: boolean;
  sidebarAvailable: boolean;
}) {
  return (
    <box
      style={{
        height: 1,
        flexDirection: 'row',
        width: '100%',
        paddingLeft: 1,
        paddingRight: 1,
        backgroundColor: '#181825',
      }}
    >
      <text fg={theme.accent}>
        <strong>◆ ALEX</strong>
      </text>
      <text fg={theme.border}>  │  </text>
      <text fg={theme.muted}>session </text>
      <text fg={theme.text}>#{truncate(sessionId || '-', 21)}</text>
      {toolCount !== undefined ? (
        <>
          <text fg={theme.border}>  ·  </text>
          <text fg={theme.tool}>{toolCount} tools</text>
        </>
      ) : null}
      <box style={{ flexGrow: 1 }} />
      {sidebarAvailable ? (
        <text fg={theme.border}>{collapsed ? '◧ collapsed' : '◨ sidebar'}  ·  Ctrl+S  </text>
      ) : null}
      <text fg={theme.user}>● ready</text>
    </box>
  );
}

const LOGO_LINES = [
  ' █████╗ ██╗     ███████╗██╗  ██╗',
  '██╔══██╗██║     ██╔════╝╚██╗██╔╝',
  '███████║██║     █████╗   ╚███╔╝ ',
  '██╔══██║██║     ██╔══╝   ██╔██╗ ',
  '██║  ██║███████╗███████╗██╔╝ ██╗',
  '╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝',
];

function Welcome({ width }: { width: number }) {
  const wide = width >= 54;
  return (
    <box
      style={{
        flexGrow: 1,
        width,
        flexDirection: 'column',
        justifyContent: 'center',
        alignItems: 'center',
        paddingBottom: 2,
      }}
    >
      {wide ? (
        <box style={{ flexDirection: 'column', alignItems: 'center' }}>
          {LOGO_LINES.map((line, index) => (
            <text key={index} fg={index < 2 ? theme.accent : '#6c8ee8'}>
              <strong>{line}</strong>
            </text>
          ))}
        </box>
      ) : (
        <text fg={theme.accent}>
          <strong>◆ ALEX</strong>
        </text>
      )}
      <text fg={theme.muted}> </text>
      <text fg={theme.border}>Type a message  ·  /help for commands  ·  Ctrl+S toggles sidebar</text>
    </box>
  );
}

function Chat({
  turns,
  width,
  thinkingExpanded,
  skillsExpanded,
  toolOutputExpanded,
  toolTurnsExpanded,
  onToggleToolTurn,
}: {
  turns: TurnView[];
  width: number;
  thinkingExpanded: boolean;
  skillsExpanded: boolean;
  toolOutputExpanded: boolean;
  toolTurnsExpanded: string[];
  onToggleToolTurn: (turnId: string) => void;
}) {
  if (turns.length === 0) return null;
  return (
    <box style={{ flexDirection: 'column' }}>
      {turns.map((turn) => (
        <TurnBlock
          key={`${turn.turnId}-${turn.userInput}`}
          turn={turn}
          width={width}
          thinkingExpanded={thinkingExpanded}
          skillsExpanded={skillsExpanded}
          toolOutputExpanded={toolOutputExpanded}
          toolGroupExpanded={toolTurnsExpanded.includes(turn.turnId)}
          onToggleToolGroup={() => onToggleToolTurn(turn.turnId)}
        />
      ))}
    </box>
  );
}

function CompactionBlock({
  part,
  width,
}: {
  part: CompactionPart;
  width: number;
}) {
  const color =
    part.status === 'failed'
      ? theme.error
      : part.status === 'running'
        ? theme.warning
        : theme.accent;
  return (
    <box
      style={{
        flexDirection: 'column',
        width,
        marginTop: 1,
        marginBottom: 1,
        border: ['left'],
        borderColor: color,
        paddingLeft: 1,
      }}
    >
      <text fg={color} style={{ width: width - 2 }}>
        <strong>⟳ {part.text}</strong>
      </text>
      {part.detail ? (
        <text fg={theme.muted} style={{ width: width - 2 }}>
          {truncate(part.detail, width - 4)}
        </text>
      ) : null}
      {part.summary ? (
        <text fg={theme.border} style={{ width: width - 2 }}>
          摘要：{truncate(part.summary.replace(/\s+/g, ' '), width - 8)}
        </text>
      ) : null}
    </box>
  );
}

function ToolCallBlock({
  tool,
  width,
  toolOutputExpanded,
  compact = false,
}: {
  tool: ToolCallView;
  width: number;
  toolOutputExpanded: boolean;
  /** Inside the process tree: no extra border/indent, just the call row. */
  compact?: boolean;
}) {
  const args = summarizeToolArgs(tool.input);
  const preview = tool.output
    ? formatToolOutput(tool.output, width - 6, toolOutputExpanded ? 14 : 3)
    : null;
  const shortOutput =
    preview !== null && preview.hidden === 0 && preview.lines.length <= 3;
  const failedTool = tool.done && tool.failed;
  const toolColor = failedTool ? theme.error : theme.tool;
  return (
    <box
      style={{
        flexDirection: 'column',
        ...(compact ? {} : { border: ['left'] as const, borderColor: toolColor, paddingLeft: 1 }),
        width,
      }}
    >
      <text fg={toolColor} style={{ width: width - 1 }}>
        ⚙ {tool.name} {tool.done ? (failedTool ? '✗' : '✓') : '…'}
        {args ? <span fg={theme.muted}> · {args}</span> : null}
      </text>
      {tool.done && preview && (shortOutput || failedTool) ? (
        <box style={{ flexDirection: 'column', width: width - 1 }}>
          {preview.lines.map((line, lineIndex) => (
            <text
              key={lineIndex}
              fg={failedTool ? theme.error : theme.muted}
              style={{ width: width - 2 }}
            >
              ┆ {line}
            </text>
          ))}
          {failedTool && preview.hidden > 0 ? (
            <text fg={theme.border} style={{ width: width - 2 }}>
              ⎿ 还有 {preview.hidden} line(s) · Ctrl+O 查看完整错误
            </text>
          ) : null}
        </box>
      ) : null}
      {tool.done && preview && !shortOutput && !failedTool ? (
        <box style={{ flexDirection: 'column', width: width - 1 }}>
          {toolOutputExpanded
            ? preview.lines.map((line, lineIndex) => (
                <text key={lineIndex} fg={theme.muted} style={{ width: width - 2 }}>
                  ┆ {line}
                </text>
              ))
            : null}
          <text fg={theme.border} style={{ width: width - 2 }}>
            {toolOutputExpanded
              ? preview.hidden > 0
                ? `┆ … ${preview.hidden} more line(s)（完整内容见 session 历史）`
                : ''
              : `⎿ ${preview.lines.length + preview.hidden} line(s) · Ctrl+O 展开`}
          </text>
        </box>
      ) : null}
    </box>
  );
}

type ProcessPart = Extract<TurnPart, { kind: 'thinking' | 'skills' | 'tool' }>;

function isProcessPart(part: TurnPart): part is ProcessPart {
  return part.kind === 'thinking' || part.kind === 'skills' || part.kind === 'tool';
}

function useSpinner(active: boolean): string {
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    if (!active) {
      setFrame(0);
      return;
    }
    const timer = setInterval(() => {
      setFrame((value) => (value + 1) % SPINNER_FRAMES.length);
    }, 120);
    return () => clearInterval(timer);
  }, [active]);
  return SPINNER_FRAMES[frame] ?? '✻';
}

function ProcessGroup({
  parts,
  width,
  marginTop,
  expanded,
  running,
  failed,
  thinkingExpanded,
  skillsExpanded,
  toolOutputExpanded,
  onToggle,
}: {
  parts: ProcessPart[];
  width: number;
  marginTop: number;
  expanded: boolean;
  running: boolean;
  failed: boolean;
  thinkingExpanded: boolean;
  skillsExpanded: boolean;
  toolOutputExpanded: boolean;
  onToggle: () => void;
}) {
  const spinner = useSpinner(running);
  const thinking = parts.filter(
    (part): part is Extract<ProcessPart, { kind: 'thinking' }> => part.kind === 'thinking',
  );
  const skills = parts.filter(
    (part): part is Extract<ProcessPart, { kind: 'skills' }> => part.kind === 'skills',
  );
  const tools = parts.filter(
    (part): part is Extract<ProcessPart, { kind: 'tool' }> => part.kind === 'tool',
  );
  const skillCount = skills.reduce((total, part) => total + part.skills.length, 0);
  const pieces: string[] = [];
  if (thinking.length > 0) pieces.push(running ? '思考中' : '思考');
  if (tools.length > 0) pieces.push(`${tools.length} 工具`);
  if (skillCount > 0) pieces.push(`${skillCount} 技能`);
  const status = failed ? 'failed' : running ? 'running' : 'done';
  const color = failed ? theme.error : running ? theme.thinking : theme.tool;

  return (
    <box style={{ flexDirection: 'column', marginTop, width }}>
      <text
        fg={color}
        style={{ width: width - 1 }}
        selectable={false}
        onMouseDown={onToggle}
      >
        {expanded ? '▾' : '▸'} {running ? spinner : '✻'} {pieces.join(' · ') || '思考'} · {status}
        {expanded ? '' : ' · click to expand'}
      </text>
      {expanded ? (
        <box style={{ flexDirection: 'column', width, paddingLeft: 0 }}>
          {parts.map((part, index) => {
            const branch = index === parts.length - 1 ? '└ ' : '├ ';
            if (part.kind === 'thinking') {
              return (
                <box key={`process-thinking-${index}`} style={{ flexDirection: 'column', width }}>
                  <text fg={theme.thinking} style={{ width: width - 1 }}>
                    {branch}💭 Thinking ({part.text.length} chars){thinkingExpanded ? '' : ' · Ctrl+T'}
                  </text>
                  {thinkingExpanded ? (
                    <box style={{ paddingLeft: 2, width: width - 2 }}>
                      <text fg={theme.thinking} style={{ width: width - 3 }}>
                        {part.text}
                      </text>
                    </box>
                  ) : null}
                </box>
              );
            }
            if (part.kind === 'skills') {
              return (
                <box key={`process-skills-${index}`} style={{ flexDirection: 'column', width }}>
                  <text fg={theme.skill} style={{ width: width - 1 }}>
                    {branch}🎯 Skills ({part.skills.length}){skillsExpanded ? '' : ' · Ctrl+K'}
                  </text>
                  {skillsExpanded
                    ? part.skills.map((skill, skillIndex) => (
                        <text key={skillIndex} fg={theme.muted} style={{ width: width - 3, paddingLeft: 2 }}>
                          ◆ {skill.name}
                          {skill.pattern ? ` · ${skill.pattern}` : ''}
                        </text>
                      ))
                    : null}
                </box>
              );
            }
            return (
              <box key={`process-tool-${part.tool.toolId}`} style={{ flexDirection: 'row', width }}>
                <text fg={theme.border}>{branch}</text>
                <ToolCallBlock
                  tool={part.tool}
                  width={width - 3}
                  toolOutputExpanded={toolOutputExpanded}
                  compact
                />
              </box>
            );
          })}
        </box>
      ) : null}
    </box>
  );
}

function TurnBlock({
  turn,
  width,
  thinkingExpanded,
  skillsExpanded,
  toolOutputExpanded,
  toolGroupExpanded,
  onToggleToolGroup,
}: {
  turn: TurnView;
  width: number;
  thinkingExpanded: boolean;
  skillsExpanded: boolean;
  toolOutputExpanded: boolean;
  toolGroupExpanded: boolean;
  onToggleToolGroup: () => void;
}) {
  if (turn.kind === 'compaction') {
    const part = turn.parts.find((item) => item.kind === 'compaction');
    return part ? <CompactionBlock part={part} width={width} /> : null;
  }
  const streaming = turn.status === 'streaming';
  const failed = turn.status === 'failed';
  const assistantColor = failed ? theme.error : theme.accent;
  const hasAssistant =
    streaming ||
    failed ||
    Boolean(turn.content) ||
    Boolean(turn.thinking) ||
    turn.skills.length > 0 ||
    turn.tools.length > 0 ||
    turn.parts.some((part) => part.kind === 'compaction');

  return (
    <box style={{ flexDirection: 'column', marginBottom: 1, width }}>
      {turn.userInput ? (
        <box
          style={{
            flexDirection: 'column',
            border: ['left'],
            borderColor: theme.user,
            paddingLeft: 1,
            marginBottom: 1,
            width,
          }}
        >
          <text fg={theme.user} style={{ width: width - 2 }}>
            <strong>❯ {turn.userInput || '(no text)'}</strong>
          </text>
          {turn.attachments.map((attachment, index) => (
            <text key={`attachment-${index}`} fg={theme.muted} style={{ width: width - 3 }}>
              {attachment.kind === 'image' ? '▣' : '▤'} {attachmentLabel(attachment)}
            </text>
          ))}
        </box>
      ) : null}

      {hasAssistant ? (
        <box
          style={{
            flexDirection: 'column',
            border: ['left'],
            borderColor: assistantColor,
            paddingLeft: 1,
            width,
          }}
        >
          <text fg={assistantColor}>
            <strong>{failed ? '✗ Alex' : '◆ Alex'}</strong>
          </text>

          {(() => {
            const parts: TurnPart[] =
              turn.parts.length > 0
                ? turn.parts
                : turn.content
                  ? [{ kind: 'text', text: turn.content }]
                  : [];
            const processParts = parts.filter(isProcessPart);
            const firstProcessIndex = parts.findIndex(isProcessPart);
            return parts.map((part, index) => {
              if (index === firstProcessIndex) {
                return (
                  <ProcessGroup
                    key="process"
                    parts={processParts}
                    width={width - 2}
                    marginTop={index > 0 ? 1 : 0}
                    expanded={
                      toolGroupExpanded ||
                      turn.error !== undefined ||
                      processParts.some((item) => item.kind === 'tool' && item.tool.done && item.tool.failed)
                    }
                    running={streaming || processParts.some((item) => item.kind === 'tool' && !item.tool.done)}
                    failed={
                      turn.error !== undefined ||
                      processParts.some((item) => item.kind === 'tool' && item.tool.done && item.tool.failed)
                    }
                    thinkingExpanded={thinkingExpanded}
                    skillsExpanded={skillsExpanded}
                    toolOutputExpanded={toolOutputExpanded}
                    onToggle={onToggleToolGroup}
                  />
                );
              }
              if (isProcessPart(part)) return null;
              if (part.kind === 'compaction') {
                return <CompactionBlock key={`compaction-${part.id}`} part={part} width={width - 2} />;
              }
              if (part.kind !== 'text') return null;
              const isLastText = index === parts.length - 1;
              return turn.status === 'streaming' && isLastText ? (
                <text key={`text-${index}`} fg={theme.text} style={{ width: width - 2 }}>
                  {formatMarkdown(part.text)}▌
                </text>
              ) : (
                <box
                  key={`text-${index}`}
                  style={{
                    marginTop: index > 0 ? 1 : 0,
                    flexDirection: 'column',
                    width: width - 2,
                  }}
                >
                  <MarkdownText text={part.text} width={width - 3} />
                </box>
              );
            });
          })()}

          {turn.error ? (
            <box
              style={{
                flexDirection: 'column',
                marginTop: 1,
                width: width - 2,
              }}
            >
              {turn.error.split('\n').map((line, index) => (
                <text key={`error-${index}`} fg={theme.error} style={{ width: width - 3 }}>
                  {line || ' '}
                </text>
              ))}
            </box>
          ) : null}
        </box>
      ) : null}
    </box>
  );
}

function shortMcpStatus(status: string): string {
  if (!status || status === 'unknown') return 'unknown';
  if (/unavailable|error|fail/i.test(status)) return 'unavailable';
  if (/ready|connected|ok/i.test(status)) return 'ready';
  return truncate(status, 14);
}

function Metric({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color: string;
}) {
  return (
    <box style={{ flexDirection: 'row', width: SIDEBAR_WIDTH - 2 }}>
      <text fg={theme.muted}>{label.padEnd(9, ' ')}</text>
      <text fg={color}>{truncate(value, 16)}</text>
    </box>
  );
}

function KeyHint({ keys, label }: { keys: string; label: string }) {
  return (
    <box style={{ flexDirection: 'row', width: SIDEBAR_WIDTH - 2 }}>
      <text fg={theme.muted}>{keys.padEnd(11, ' ')}</text>
      <text fg={theme.border}>{label}</text>
    </box>
  );
}

function shortenRoot(root: string): string {
  if (!root) return '(unavailable)';
  const home = process.env.USERPROFILE ?? process.env.HOME ?? '';
  const normalizedRoot = root.replace(/\\/g, '/');
  const normalizedHome = home.replace(/\\/g, '/').replace(/\/$/, '');
  if (normalizedHome && normalizedRoot === normalizedHome) return '~';
  if (normalizedHome && normalizedRoot.startsWith(`${normalizedHome}/`)) {
    return `~/${normalizedRoot.slice(normalizedHome.length + 1)}`;
  }
  return normalizedRoot;
}

function Sidebar({
  sidebar,
  sessionId,
  reflection,
  tab,
  agents,
  viewAgentId,
  onAgentSelect,
  onAgentDispose,
  fileTree,
  fileRows,
  fileSelected,
  onTabSelect,
}: {
  sidebar: TuiSnapshot['sidebar'];
  sessionId: string;
  reflection: TuiSnapshot['reflection'];
  tab: TuiSnapshot['sidebarTab'];
  agents: TuiSnapshot['sidebarAgents'];
  viewAgentId: string;
  onAgentSelect: (agentId: string) => void;
  onAgentDispose: (agentId: string) => void;
  fileTree: TuiSnapshot['fileTree'];
  fileRows: TuiSnapshot['fileRows'];
  fileSelected: number;
  onTabSelect: (tab: TuiSnapshot['sidebarTab']) => void;
}) {
  const treeRef = React.useRef<{ scrollTop: number } | null>(null);
  useEffect(() => {
    if (treeRef.current) {
      treeRef.current.scrollTop = Math.max(0, fileSelected - 4);
    }
  }, [fileSelected, fileRows.length]);
  const statusColor: Record<string, string> = {
    M: theme.warning,
    A: theme.user,
    '?': theme.user,
    D: theme.error,
    R: theme.skill,
    U: theme.error,
  };
  const reflectionText = reflection
    ? `+${reflection.created} ~${reflection.updated} -${reflection.deprecated}`
    : 'idle';
  return (
    <box
      style={{
        width: SIDEBAR_WIDTH,
        flexShrink: 0,
        border: ['left'],
        borderColor: theme.border,
        paddingLeft: 1,
        flexDirection: 'column',
      }}
    >
      <box
        style={{
          flexDirection: 'row',
          width: SIDEBAR_WIDTH - 2,
          backgroundColor: '#181825',
        }}
      >
        <box
          onMouseDown={() => onTabSelect('overview')}
          style={{
            paddingLeft: 1,
            paddingRight: 1,
            backgroundColor: tab === 'overview' ? '#313244' : undefined,
          }}
        >
          <text fg={tab === 'overview' ? theme.accent : theme.muted}>
            <strong>STATUS</strong>
          </text>
        </box>
        <box
          onMouseDown={() => onTabSelect('files')}
          style={{
            paddingLeft: 1,
            paddingRight: 1,
            backgroundColor: tab === 'files' ? '#313244' : undefined,
          }}
        >
          <text fg={tab === 'files' ? theme.accent : theme.muted}>
            <strong>FILES</strong>
          </text>
        </box>
        <box
          onMouseDown={() => onTabSelect('agents')}
          style={{
            paddingLeft: 1,
            paddingRight: 1,
            backgroundColor: tab === 'agents' ? '#313244' : undefined,
          }}
        >
          <text fg={tab === 'agents' ? theme.accent : theme.muted}>
            <strong>AGENTS</strong>
          </text>
        </box>
      </box>

      {tab === 'agents' ? (
        <scrollbox
          scrollX={false}
          style={{ flexGrow: 1, flexShrink: 1, flexBasis: 0, width: '100%' }}
          scrollbarOptions={{ visible: true }}
        >
          <box style={{ flexDirection: 'column', width: SIDEBAR_WIDTH - 2 }}>
            <text fg={theme.accent}>
              <strong>CONVERSATION AGENT ({agents.length})</strong>
            </text>
            <text fg={theme.muted}>click to switch · × to dispose</text>
            <box style={{ height: 1 }} />
            {agents.length === 0 ? (
              <text fg={theme.muted}>no agents</text>
            ) : (
              agents.map((agent) => {
                const selected = agent.agentId === viewAgentId;
                return (
                  <box
                    key={agent.agentId}
                    onMouseDown={() => onAgentSelect(agent.agentId)}
                    style={{
                      flexDirection: 'row',
                      paddingLeft: 1,
                      paddingRight: 1,
                      backgroundColor: selected ? '#313244' : undefined,
                    }}
                  >
                    <text fg={selected ? theme.accent : theme.text} style={{ width: 16 }}>
                      {selected ? '▸ ' : '  '}
                      {'  '.repeat(agent.depth)}
                      {truncate(agent.agentId, Math.max(6, 13 - agent.depth * 2))}
                    </text>
                    <text fg={theme.muted} style={{ width: 9 }}>
                      {agent.role.slice(0, 5)} {agent.status === 'running' ? '●' : '○'}
                    </text>
                    {agent.agentId !== 'main' ? (
                      <text
                        fg={theme.error}
                        onMouseDown={(event) => {
                          event.stopPropagation?.();
                          onAgentDispose(agent.agentId);
                        }}
                      >
                        ×
                      </text>
                    ) : null}
                  </box>
                );
              })
            )}
            <box style={{ height: 1 }} />
            <text fg={theme.muted}>view: {truncate(viewAgentId, 20)}</text>
            <KeyHint keys="F3" label="agents tab" />
          </box>
        </scrollbox>
      ) : tab === 'overview' ? (
        <scrollbox
          scrollX={false}
          style={{ flexGrow: 1, flexShrink: 1, flexBasis: 0, width: '100%' }}
          scrollbarOptions={{ visible: false }}
        >
          <box style={{ flexDirection: 'column', width: SIDEBAR_WIDTH - 2 }}>
            <text fg={theme.accent}>
              <strong>SESSION</strong>
            </text>
            <text fg={theme.text}>#{truncate(sessionId || '-', 24)}</text>
            <text fg={theme.user}>● active</text>
            <text fg={theme.border}>──────────────────────────</text>

            <text fg={theme.accent}>
              <strong>RUNTIME</strong>
            </text>
            <Metric label="tools" value={String(sidebar.tools)} color={theme.tool} />
            <Metric label="skills" value={String(sidebar.skills)} color={theme.skill} />
            <Metric label="cron" value={String(sidebar.cron)} color={theme.text} />
            <Metric
              label="mcp"
              value={shortMcpStatus(sidebar.mcpStatus)}
              color={
                /unavailable|error|fail/i.test(sidebar.mcpStatus)
                  ? theme.warning
                  : /ready|connected|ok/i.test(sidebar.mcpStatus)
                    ? theme.user
                    : theme.muted
              }
            />
            <Metric
              label="reflect"
              value={reflectionText}
              color={reflection ? theme.accent : theme.muted}
            />
            <text fg={theme.border}>──────────────────────────</text>

            <text fg={theme.accent}>
              <strong>KEYS</strong>
            </text>
            <KeyHint keys="Ctrl+C" label="copy / quit" />
            <KeyHint keys="Ctrl+G/B" label="feedback" />
            <KeyHint keys="Ctrl+T/K/O" label="expand" />
            <KeyHint keys="F1-F3" label="sidebar tabs" />
            <KeyHint keys="Ctrl+S" label="collapse" />
            <KeyHint keys="/help" label="commands" />
            <KeyHint keys=":q" label="close page" />
          </box>
        </scrollbox>
      ) : (
        <>
          <box style={{ flexDirection: 'column', width: SIDEBAR_WIDTH - 2 }}>
            <text fg={theme.muted}>ROOT</text>
            <text fg={theme.text}>{truncate(shortenRoot(fileTree.root), SIDEBAR_WIDTH - 4)}</text>
            <text fg={fileTree.isGitRepo ? theme.user : theme.border}>
              {fileTree.isGitRepo
                ? `git ✓ ${Object.keys(fileTree.gitStatus).length} changed`
                : 'not a repo'}
            </text>
          </box>
          <text fg={theme.border}>──────────────────────────</text>
          <scrollbox
            ref={treeRef as never}
            scrollX={false}
            style={{ flexGrow: 1, flexShrink: 1, flexBasis: 0, width: '100%' }}
            scrollbarOptions={{
              visible: true,
              trackOptions: {
                backgroundColor: '#181825',
                foregroundColor: theme.border,
              },
            }}
            horizontalScrollbarOptions={{ visible: false }}
          >
            <box style={{ flexDirection: 'column', width: SIDEBAR_WIDTH - 2 }}>
              {fileRows.length === 0 ? (
                <text fg={theme.muted}>无文件</text>
              ) : (
                fileRows.map((row, index) => {
                  const selected = index === fileSelected;
                  const indent = '  '.repeat(row.depth);
                  const caret = row.type === 'dir' ? (row.expanded ? '▾ ' : '▸ ') : '· ';
                  const statusWidth = row.gitStatus ? 2 : 0;
                  const lead = selected ? '❯ ' : '  ';
                  const nameWidth = Math.max(
                    4,
                    SIDEBAR_WIDTH - 2 - lead.length - indent.length - 2 - statusWidth,
                  );
                  return (
                    <box
                      key={`${row.path}-${index}`}
                      style={{
                        flexDirection: 'row',
                        width: SIDEBAR_WIDTH - 2,
                        backgroundColor: selected ? '#313244' : undefined,
                      }}
                    >
                      <text
                        fg={
                          row.type === 'dir'
                            ? selected
                              ? theme.accent
                              : theme.text
                            : selected
                              ? theme.text
                              : theme.muted
                        }
                      >
                        {lead}
                        {indent}
                        {caret}
                        {truncate(row.name, nameWidth)}
                      </text>
                      <box style={{ flexGrow: 1 }} />
                      {row.gitStatus ? (
                        <text fg={statusColor[row.gitStatus] ?? theme.muted}>
                          {row.gitStatus}{' '}
                        </text>
                      ) : null}
                    </box>
                  );
                })
              )}
              {fileTree.truncated ? <text fg={theme.warning}>… truncated</text> : null}
            </box>
          </scrollbox>
          <text fg={theme.border}>↑↓ →← Enter · F5 · F1/F2</text>
        </>
      )}
    </box>
  );
}

function InlineSettingInput({
  value,
  width,
  placeholder,
  onInput,
  onSubmit,
}: {
  value: string;
  width: number;
  placeholder?: string;
  onInput: (value: string) => void;
  onSubmit: (value: string) => void;
}) {
  const ref = useRef<InputRenderable | null>(null);
  useEffect(() => {
    ref.current?.focus();
    ref.current?.selectAll();
  }, []);
  return (
    <input
      ref={ref}
      style={{ width }}
      value={value}
      focused
      placeholder={placeholder ?? ''}
      onInput={onInput}
      onSubmit={(value: any) => onSubmit(String(value))}
    />
  );
}

function PagePanel({
  page,
  width,
  selectedIndex,
  editingKey,
  draft,
  onBeginEdit,
  onActivateField,
  onDraftChange,
  onSaveEdit,
  onMcpToggle,
  pendingTeamId,
  onTeamDispose,
}: {
  page: NonNullable<TuiSnapshot['page']>;
  width: number;
  selectedIndex: number;
  editingKey?: string;
  draft: string;
  onBeginEdit: (field: TuiSettingField, index: number) => void;
  onActivateField: (field: TuiSettingField, index: number) => void;
  onDraftChange: (value: string) => void;
  onSaveEdit: (value: string) => void;
  onMcpToggle: (index: number, enabled: boolean) => void;
  pendingTeamId: string | null;
  onTeamDispose: (teamId: string) => void;
}) {
  const fields = page.kind === 'settings' ? page.fields ?? [] : [];
  const labelWidth = Math.min(
    36,
    Math.max(24, ...fields.map((field) => field.label.length + 4)),
  );
  const valueWidth = Math.max(8, width - 4 - labelWidth - 2);
  return (
    <box
      style={{
        flexDirection: 'column',
        flexGrow: 1,
        flexShrink: 1,
        flexBasis: 0,
        minHeight: 0,
        overflow: 'hidden',
        border: true,
        borderColor: theme.border,
        paddingLeft: 1,
        paddingRight: 1,
        marginTop: 1,
        width,
      }}
    >
      <text fg={theme.accent}>
        <strong>{page.title}</strong>
      </text>
      <scrollbox
        scrollX={false}
        style={{ flexGrow: 1, flexShrink: 1, flexBasis: 0, width: '100%' }}
        scrollbarOptions={{
          visible: true,
          trackOptions: {
            backgroundColor: '#181825',
            foregroundColor: theme.border,
          },
        }}
        horizontalScrollbarOptions={{ visible: false }}
      >
        <box style={{ flexDirection: 'column', width: width - 4 }}>
          {fields.length > 0
            ? fields.map((field, index) => {
                const editing = field.key === editingKey;
                const selected = editing || index === selectedIndex;
                const actionable = field.kind === 'toggle' || field.kind === 'choice';
                return (
                  <box
                    key={field.key}
                    style={{ flexDirection: 'row', width: width - 4, height: 1 }}
                  >
                    <text
                      fg={selected ? theme.accent : theme.muted}
                      style={{ width: labelWidth }}
                      selectable={false}
                      onMouseDown={() =>
                        actionable
                          ? onActivateField(field, index)
                          : onBeginEdit(field, index)
                      }
                    >
                      {selected ? '❯ ' : '  '}
                      {truncate(field.label, labelWidth - 2)}
                    </text>
                    {editing && !actionable ? (
                      <InlineSettingInput
                        value={draft}
                        width={valueWidth}
                        placeholder={field.secret ? '输入新值（留空取消）' : field.value}
                        onInput={onDraftChange}
                        onSubmit={onSaveEdit}
                      />
                    ) : field.kind === 'toggle' ? (
                      <text
                        fg={field.value === 'on' ? theme.user : theme.muted}
                        style={{ width: valueWidth }}
                        selectable={false}
                        onMouseDown={() => onActivateField(field, index)}
                      >
                        {field.value === 'on' ? '● ON' : '○ OFF'}
                      </text>
                    ) : field.kind === 'choice' ? (
                      <text
                        fg={selected ? theme.warning : theme.muted}
                        style={{ width: valueWidth }}
                        selectable={false}
                        onMouseDown={() => onActivateField(field, index)}
                      >
                        {`‹ ${field.value} ›`}
                      </text>
                    ) : (
                      <text
                        fg={selected ? theme.accent : theme.text}
                        style={{ width: valueWidth }}
                        selectable={false}
                        onMouseDown={() => onBeginEdit(field, index)}
                      >
                        {truncate(field.value, valueWidth)}
                      </text>
                    )}
                  </box>
                );
              })
            : page.body.split('\n').map((line, index) => {
                if (page.kind === 'context' && line.startsWith('◆')) {
                  return (
                    <text key={index} fg={theme.accent} selectable={false}>
                      <strong>{line}</strong>
                    </text>
                  );
                }
                const bar = page.kind === 'context' ? line.match(/^(\s*)([█░]+)(.*)$/) : null;
                if (bar) {
                  return (
                    <box
                      key={index}
                      style={{ flexDirection: 'row', width: width - 4, height: 1 }}
                    >
                      <text fg={theme.text}>{bar[1]}</text>
                      <text fg={theme.accent} selectable={false}>
                        {bar[2]}
                      </text>
                      <text fg={theme.text}>{truncate(bar[3]!, Math.max(8, width - 34))}</text>
                    </box>
                  );
                }
                const team = page.kind === 'teams'
                  ? line.match(/^-\s+(\S+):(.*?)\s*\[解散\]$/)
                  : null;
                if (team) {
                  const confirming = pendingTeamId === team[1];
                  const stats = Object.fromEntries(
                    [...team[2]!.matchAll(/(\w+)=(\d+)/g)].map((match) => [match[1], match[2]]),
                  );
                  const summary =
                    `agents ${stats.agents ?? '0'} · tasks ${stats.tasks ?? '0'}` +
                    (Number(stats.running ?? 0) > 0 ? ` · running ${stats.running}` : '') +
                    ` · done ${stats.completed ?? '0'}`;
                  const prefixWidth = Math.max(24, width - 22);
                  return (
                    <box
                      key={index}
                      style={{ flexDirection: 'row', width: width - 4, height: 1 }}
                    >
                      <text fg={theme.accent} style={{ width: 2 }}>
                        ◆
                      </text>
                      <text fg={theme.text} style={{ width: prefixWidth - 2 }}>
                        {truncate(` ${team[1]}  ${summary}`, prefixWidth - 3)}
                      </text>
                      <text
                        fg={confirming ? theme.warning : theme.error}
                        selectable={false}
                        onMouseDown={() => onTeamDispose(team[1]!)}
                      >
                        {confirming ? '[ 确认解散 ]' : '[ 解散 ]'}
                      </text>
                    </box>
                  );
                }
                const row = page.kind === 'mcp' ? line.match(/^(\d+)\.\s/) : null;
                const toggle = page.kind === 'mcp' ? line.match(/^(.*?)(\[ ON \]|\[ OFF \])(.*)$/) : null;
                if (row && toggle) {
                  const switchOn = toggle[2] === '[ ON ]';
                  const switchLabel = switchOn ? '● ON ' : '○ OFF';
                  const prefixWidth = Math.min(44, Math.max(20, width - 46));
                  return (
                    <box
                      key={index}
                      style={{ flexDirection: 'row', width: width - 4, height: 1 }}
                    >
                      <text fg={theme.text} style={{ width: prefixWidth }}>
                        {truncate(toggle[1]!, prefixWidth - 1)}
                      </text>
                      <text
                        fg={switchOn ? theme.user : theme.muted}
                        selectable={false}
                        onMouseDown={() =>
                          onMcpToggle(Number.parseInt(row[1]!, 10), !switchOn)
                        }
                      >
                        {switchLabel}
                      </text>
                      <text
                        fg={theme.text}
                        selectable={false}
                        style={{
                          width: Math.max(8, width - 6 - prefixWidth - switchLabel.length),
                        }}
                      >
                        {truncate(
                          toggle[3]!,
                          Math.max(8, width - 8 - prefixWidth - switchLabel.length),
                        )}
                      </text>
                    </box>
                  );
                }
                return (
                  <text
                    key={index}
                    fg={theme.text}
                    style={{ width: width - 4 }}
                    selectable={false}
                  >
                    {truncate(line, width - 6) || ' '}
                  </text>
                );
              })}
        </box>
      </scrollbox>
      <text fg={theme.muted}>
        {page.kind === 'settings'
          ? editingKey
            ? 'Enter save · Esc cancel'
            : '↑/↓ select · Enter 切换/编辑 · Esc close'
          : page.kind === 'mcp'
            ? '点击 [ ON ] / [ OFF ] 切换 · 数字展开 · Esc close'
            : page.kind === 'context'
              ? '/compact 手动压缩 · Esc close'
              : ':q / Esc close'}
      </text>
    </box>
  );
}

function ApprovalPanel({
  approval,
  preview,
  offset,
  visibleLines,
  width,
}: {
  approval: ApprovalView;
  preview: ReturnType<typeof formatApprovalPreview>;
  offset: number;
  visibleLines: number;
  width: number;
}) {
  const loaded = preview.lines.length;
  const start = Math.min(offset, Math.max(0, loaded - visibleLines));
  const visible = preview.lines.slice(start, start + visibleLines);
  const end = Math.min(loaded, start + visible.length);
  const truncNote = preview.truncated ? `（另有 ${preview.total - loaded} 行未加载）` : '';
  const counter = loaded > 0 ? `行 ${start + 1}-${end}/${loaded}${truncNote}` : '空预览';
  return (
    <box
      style={{
        flexDirection: 'column',
        flexGrow: 1,
        flexShrink: 1,
        flexBasis: 0,
        minHeight: 0,
        overflow: 'hidden',
        border: true,
        borderColor: theme.warning,
        paddingLeft: 1,
        paddingRight: 1,
        marginTop: 1,
        width,
      }}
    >
      <text fg={theme.warning}>
        <strong>⚠ Tool approval required</strong>
      </text>
      <text fg={theme.text}>
        {approval.toolName} · permission: {approval.permission} · {preview.total} line(s)
      </text>
      <box style={{ flexDirection: 'column', flexGrow: 1, overflow: 'hidden', width: width - 4 }}>
        {visible.map((line, index) => (
          <text
            key={start + index}
            fg={approvalLineColor(line)}
            style={{ width: width - 4 }}
          >
            {line || ' '}
          </text>
        ))}
      </box>
      <text fg={theme.muted}>{counter} · ↑↓ PgUp/PgDn Home/End 滚动</text>
      <text fg={theme.accent}>y approve · s approve &amp; remember · n deny</text>
    </box>
  );
}

function StatusBar({ snapshot, width }: { snapshot: TuiSnapshot; width: number }) {
  const streaming = snapshot.streaming.length > 0;
  const statusColor = streaming ? theme.warning : theme.user;
  const modeColor =
    snapshot.permissionMode === 'full'
      ? theme.user
      : snapshot.permissionMode === 'model'
        ? theme.skill
        : snapshot.permissionMode === 'read-only'
          ? theme.muted
          : theme.accent;
  const ctx = snapshot.contextPercent;
  const ctxColor = ctx === null ? theme.muted : ctx >= 80 ? theme.error : ctx >= 60 ? theme.warning : theme.accent;
  const cache = snapshot.cacheHitRate;
  const cacheColor =
    cache === null ? theme.muted : cache >= 50 ? theme.user : cache >= 20 ? theme.warning : theme.error;
  const model = truncate(snapshot.model || '-', Math.max(10, Math.min(28, width - 56)));
  return (
    <box
      style={{
        height: 2,
        flexDirection: 'row',
        alignItems: 'center',
        paddingLeft: 2,
        border: ['top'],
        borderColor: theme.border,
      }}
    >
      <text fg={theme.muted}>
        <span fg={statusColor}>{streaming ? '●' : '○'}</span>
        {' '}
        <span fg={statusColor}>{streaming ? 'streaming' : 'idle'}</span>
        <span fg={theme.border}> │ </span>
        <span fg={theme.accent}>◆</span>
        {' '}
        <span fg={theme.text}>{model}</span>
        <span fg={theme.border}> │ </span>
        <span fg={theme.muted}>effort</span>
        {' '}
        <span fg={theme.warning}>{snapshot.effort}</span>
        <span fg={theme.border}> │ </span>
        <span fg={theme.muted}>ctx</span>
        {' '}
        <span fg={ctxColor}>{ctx === null ? '-' : `${ctx}%`}</span>
        <span fg={theme.border}> │ </span>
        <span fg={theme.muted}>cache</span>
        {' '}
        <span fg={cacheColor}>{cache === null ? '-' : `${cache.toFixed(1)}%`}</span>
        <span fg={theme.border}> │ </span>
        <span fg={modeColor}>perm {permissionModeLabel(snapshot.permissionMode)}</span>
        <span fg={theme.border}> ⇥</span>
      </text>
    </box>
  );
}

function permissionModeLabel(mode: TuiSnapshot['permissionMode']): string {
  if (mode === 'read-only') return '只读';
  if (mode === 'model') return '模型';
  if (mode === 'full') return '完全';
  return '手动';
}

function approvalLineColor(line: string): string {
  if (line.startsWith('@@')) return theme.accent;
  if (line.startsWith('+++') || line.startsWith('---')) return theme.muted;
  if (line.startsWith('+')) return theme.user;
  if (line.startsWith('-')) return theme.error;
  return theme.text;
}

function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max)}…`;
}