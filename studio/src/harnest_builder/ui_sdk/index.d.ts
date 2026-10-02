/** Studio UI v1. Type-only imports add no browser runtime or framework dependency. */
export type Dispose = () => void;
export type JSONValue = null | boolean | number | string | JSONValue[] | { [key: string]: JSONValue };
export type HTTPMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE' | 'HEAD' | 'OPTIONS';
export type Slot = 'shell' | 'service' | 'welcome' | 'workspace.tabs' | 'inspector.sections';
export type Region = 'sidebar' | 'workspace' | 'inspector';

export interface ProjectState { id: string; name: string; framework: string }
export interface ConnectionState { ready: boolean; message: string }
export interface Job {
  id: string;
  project: string;
  command: string[];
  args: string[];
  argv: string[];
  status: 'running' | 'succeeded' | 'failed' | 'stopped';
  exit_code: number | null;
  output: string;
  serving: boolean;
  started: number;
}
export interface CommandRequest {
  action: 'init' | 'add' | 'compile' | 'test' | 'smoke' | 'eval' | 'sync' | 'serve' | 'run' | 'install-extension' | 'search-extensions' | 'provision';
  operation?: 'init' | 'plan' | 'apply' | 'status' | 'stop' | 'remove' | 'history' | 'rollback';
  revision?: number;
  manifest_revision?: string;
  environment?: string;
  project?: string;
  directory?: string;
  name?: string;
  kind?: string;
  metric?: string;
  framework?: string;
  mode?: string;
  profile?: string;
  environment_profile?: 'runtime' | 'compile' | 'development' | 'eval';
  eval_trajectory?: 'business' | 'strict';
  url?: string;
  token_env?: string;
  via?: string;
  port?: number;
  input?: string;
}
export interface ProjectDraft {
  prompt?: string; model?: string; name?: string; directory?: string;
  framework?: string; mode?: string; profile?: string;
}
export interface FileChange { path: string; text: string; revision: string; before?: string }
export interface Proposal { summary: string; files: FileChange[]; kind?: 'message'; model?: string }

/** Augment this interface to register company commands with checked arguments and results. */
export interface StudioCommandMap {
  'studio.project.create': (options?: ProjectDraft) => void;
  'studio.project.refresh': () => Promise<void>;
  'studio.file.open': (path: string) => Promise<void>;
  'studio.editor.isDirty': () => boolean;
  'studio.jobs.run': (body: CommandRequest) => Promise<Job>;
  'studio.notice': (message: string) => void;
  'studio.proposal.review': (project: string, proposal: Proposal) => void;
  'studio.appearance': () => void;
  'studio.deployment.open': () => Promise<void>;
}
export interface Commands {
  register<K extends keyof StudioCommandMap>(name: K, handler: StudioCommandMap[K]): Dispose;
  execute<K extends keyof StudioCommandMap>(name: K, ...args: Parameters<StudioCommandMap[K]>): ReturnType<StudioCommandMap[K]>;
}
/** State may be absent until its publisher runs; subscriptions replay the latest snapshot. */
export interface StudioStateMap {
  project: ProjectState | null;
  connection: ConnectionState;
  jobs: Job[];
}
export interface State {
  publish<K extends keyof StudioStateMap>(name: K, value: StudioStateMap[K]): void;
  subscribe<K extends keyof StudioStateMap>(name: K, listener: (value: StudioStateMap[K]) => void): Dispose;
}
export interface Contribution {
  id: string; title: string; slot: Slot; entry: string; mode: 'module' | 'frame';
  replaces: string | null; permissions: ('state.project' | 'state.connection')[];
  pack: string; base: string;
}
export type SlotAdapter = (host: HTMLElement, item: Contribution) => void | Dispose;
export interface Slots {
  register(name: Slot, adapter: SlotAdapter): Dispose;
  attach(name: Slot, host: HTMLElement, item: Contribution): void | Dispose;
}
export interface Theme { id: string; title: string; tokens: Record<string, string>; pack: string; base: string }
export interface Layout { id: string; title: string; order: Region[]; hidden: ('sidebar' | 'inspector')[]; pack: string; base: string }
export interface Pack { id: string; version: string; digest: string; trusted: boolean }
export interface AppearanceSelection { theme?: string; layout?: string }
export interface AppearanceCatalog { themes: Theme[]; layouts: Layout[]; packs: Pack[] }
export interface Appearance {
  catalog(): AppearanceCatalog;
  current(): Required<AppearanceSelection>;
  preview(selection: AppearanceSelection): Required<AppearanceSelection>;
  save(selection: AppearanceSelection): Required<AppearanceSelection>;
  reset(): Required<AppearanceSelection>;
}
export interface StudioContext {
  readonly version: 1;
  /** Path is relative to /api/. Supply a result type when the endpoint shape is known. */
  request<T = unknown>(path: string, method?: HTTPMethod, body?: JSONValue | CommandRequest): Promise<T>;
  /** Streaming/raw responses use the full same-origin /api/... path. */
  transport(path: `/api/${string}`, options?: RequestInit): Promise<Response>;
  authorize(launchURL?: string): void;
  readonly state: State;
  readonly commands: Commands;
  readonly slots: Slots;
  readonly navigation: { guard(handler: () => boolean): Dispose };
  readonly appearance: Appearance;
  mountHTML(host: HTMLElement, path: string): Promise<void>;
}
/** Service contributions receive null; visual contributions receive a dedicated container. */
export type Activate = (studio: StudioContext, host: HTMLElement | null) => void | Dispose | Promise<void | Dispose>;
export type ViewActivate = (studio: StudioContext, host: HTMLElement) => void | Dispose | Promise<void | Dispose>;
export type ServiceActivate = (studio: StudioContext, host: null) => void | Dispose | Promise<void | Dispose>;
