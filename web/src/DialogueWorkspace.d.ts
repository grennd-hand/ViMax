import type {ComponentType} from 'react';

declare const DialogueWorkspace: ComponentType<{
  runId?: string;
  startFresh?: boolean;
  selectionVersion?: number;
  onRunChanged?: (runId: string) => void;
}>;
export default DialogueWorkspace;
