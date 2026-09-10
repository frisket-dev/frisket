// The React-facing adapter over dispatch(): given a CommandContext (assembled by
// bind/useCommandContext from the per-project handles), returns a stable
// callback that dispatches typed WorkspaceCommands. bind/ is the only layer
// allowed to touch React; core/commands/registry stays framework-free.

import { useCallback } from 'react';
import { dispatch } from '../core/commands/registry';
import type { CommandContext, WorkspaceCommand } from '../core/commands/types';

export type DispatchCommand = (command: WorkspaceCommand) => void | Promise<void>;

export function useCommand(ctx: CommandContext): DispatchCommand {
  return useCallback((command: WorkspaceCommand) => dispatch(command, ctx), [ctx]);
}
