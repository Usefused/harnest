import type { Activate, ServiceActivate, ViewActivate, StudioContext, Job, CommandRequest } from '@harnest/studio-ui';

declare module '@harnest/studio-ui' {
  interface StudioCommandMap { 'acme.deploy': (environment: 'staging' | 'production') => Promise<string> }
  interface StudioStateMap { 'acme.deployment': { url: string } }
}

export const activate: Activate = async (studio, host) => {
  const stop = studio.state.subscribe('project', project => {
    if (host) host.textContent = project?.name ?? 'Choose a project';
  });
  const release = studio.commands.register('acme.deploy', async environment => environment);
  const url: string = await studio.commands.execute('acme.deploy', 'staging');
  studio.state.publish('acme.deployment', { url });
  const dirty: boolean = studio.commands.execute('studio.editor.isDirty');
  const command: CommandRequest = { action: 'provision', operation: 'plan' };
  const job: Job = await studio.commands.execute('studio.jobs.run', command);
  const response = await studio.request<Job>('command', 'POST', command);
  const raw: Response = await studio.transport('/api/agui', { method: 'POST', body: '{}' });
  const detach = studio.slots.register('workspace.tabs', (container, item) => {
    container.textContent = item.title;
    return () => container.replaceChildren();
  });
  studio.appearance.save({ theme: 'acme/dark' });
  const guard = studio.navigation.guard(() => !dirty);
  if (host) await studio.mountHTML(host, './panel.html');
  void [job, response, raw];
  return () => { stop(); release(); detach(); guard(); };
};

export const service: ServiceActivate = (studio, host) => {
  const empty: null = host;
  void empty;
  return studio.commands.register('studio.notice', message => { console.log(message); });
};
export const view: ViewActivate = (_, host) => { host.replaceChildren(); };

export function invalid(studio: StudioContext): void {
  // @ts-expect-error Misspelled methods cannot disappear into any.
  studio.commands.exeucte('studio.deployment.open');
  // @ts-expect-error Built-in commands enforce their argument types.
  studio.commands.execute('studio.file.open', 42);
  // @ts-expect-error Command arguments cannot be omitted.
  studio.commands.execute('studio.file.open');
  // @ts-expect-error Unknown command names require explicit declaration merging.
  studio.commands.execute('acme.missing');
  // @ts-expect-error Registration preserves return types.
  studio.commands.register('studio.editor.isDirty', () => 'yes');
  // @ts-expect-error Company commands retain their own types.
  studio.commands.execute('acme.deploy', 'unknown');
  // @ts-expect-error State shape is checked.
  studio.state.publish('connection', { ready: 'yes', message: '' });
  // @ts-expect-error Custom state shape is checked too.
  studio.state.publish('acme.deployment', { url: 123 });
  // @ts-expect-error Slot names are finite.
  studio.slots.register('workspace.missing', () => {});
  // @ts-expect-error Requests accept HTTP methods, not arbitrary strings.
  studio.request('workspace', 'FETCH');
  // @ts-expect-error JSON requests cannot contain functions.
  studio.request('workspace', 'POST', { callback: () => {} });
  // @ts-expect-error Raw transport requires the API prefix.
  studio.transport('agui');
  // @ts-expect-error Navigation guards must be synchronous.
  studio.navigation.guard(async () => true);
  // @ts-expect-error Services have no element to mount into.
  studio.mountHTML(null, './panel.html');
  // @ts-expect-error Built-in job actions are checked.
  studio.commands.execute('studio.jobs.run', { action: 'shell' });
}
