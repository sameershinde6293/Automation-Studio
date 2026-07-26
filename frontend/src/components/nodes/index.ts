import type { NodeTypes } from 'reactflow';
import { StartNode } from './StartNode';
import { EndNode } from './EndNode';
import { AIChatNode } from './AIChatNode';
import { AICompletionNode } from './AICompletionNode';
import { PromptNode } from './PromptNode';
import { VariableNode } from './VariableNode';
import { ConditionNode } from './ConditionNode';
import { LoopNode } from './LoopNode';
import { DelayNode } from './DelayNode';
import { HTTPRequestNode } from './HTTPRequestNode';
import { WebhookNode } from './WebhookNode';
import { PythonNode } from './PythonNode';
import { JavaScriptNode } from './JavaScriptNode';
import { DatabaseNode } from './DatabaseNode';
import { EmailNode } from './EmailNode';
import { FileNode } from './FileNode';
import { FolderNode } from './FolderNode';
import { ImageGenerationNode } from './ImageGenerationNode';
import { TTSNode } from './TTSNode';
import { STTNode } from './STTNode';
import { FFmpegNode } from './FFmpegNode';
import { MediaProcessingNode } from './MediaProcessingNode';

export const nodeTypes: NodeTypes = {
  start: StartNode,
  end: EndNode,
  aiChat: AIChatNode,
  aiCompletion: AICompletionNode,
  prompt: PromptNode,
  variable: VariableNode,
  condition: ConditionNode,
  loop: LoopNode,
  delay: DelayNode,
  httpRequest: HTTPRequestNode,
  webhook: WebhookNode,
  python: PythonNode,
  javascript: JavaScriptNode,
  database: DatabaseNode,
  email: EmailNode,
  file: FileNode,
  folder: FolderNode,
  imageGeneration: ImageGenerationNode,
  tts: TTSNode,
  stt: STTNode,
  ffmpeg: FFmpegNode,
  mediaProcessing: MediaProcessingNode,
};