export type SessionSummary = {
  dialogueRunId?: string;
  sessionId: string;
  projectName: string;
  workingDir: string;
  stage: string;
  summary: string;
  idea: string;
  updatedAt: string;
  createdAt: string;
  compactionTurns: number;
};

export type ConfigSection = {
  model_provider?: string;
  model: string;
  base_url: string;
  api_key: string;
  has_api_key: boolean;
};

export type AgentConfig = {
  sections: {llm: ConfigSection};
};
