import type {ComponentType} from 'react';

declare const DialogueWorkspace: ComponentType<{
  runId?: string;
  startFresh?: boolean;
  onRunChanged?: (runId: string) => void;
}>;
export default DialogueWorkspace;
