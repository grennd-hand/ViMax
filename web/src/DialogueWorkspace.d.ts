import type {ComponentType} from 'react';

declare const DialogueWorkspace: ComponentType<{
  runId?: string;
  onRunChanged?: (runId: string) => void;
}>;
export default DialogueWorkspace;
