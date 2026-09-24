import { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  CopilotChat,
  CopilotKitProvider,
  HttpAgent,
  useAgent,
  useCopilotKit,
  useFrontendTool,
  useInterrupt,
} from "@copilotkit/react-core/v2";
import { z } from "zod";
import "@copilotkit/react-core/v2/styles.css";
import "./styles.css";

type Framework = "adk" | "langgraph";
const scenarios = [
  [
    "hello",
    "01",
    "Conversation",
    "Send a message and watch the response arrive.",
  ],
  ["theme", "02", "Browser tool", "Let the agent change this page’s accent."],
  [
    "approve",
    "03",
    "Human approval",
    "Approve or decline a fictional publication.",
  ],
  [
    "error",
    "04",
    "Error recovery",
    "Trigger a failure, then send another message.",
  ],
] as const;

function Workspace() {
  const initial = new URLSearchParams(location.search).get("framework");
  const [framework, setFramework] = useState<Framework>(
    initial === "langgraph" ? "langgraph" : "adk",
  );
  const [thread, setThread] = useState(() => crypto.randomUUID());
  const [error, setError] = useState("");
  const agents = useMemo(
    () => ({
      default: new HttpAgent({
        url: `/api/${framework}/agui`,
        threadId: thread,
      }),
    }),
    [framework, thread],
  );
  return (
    <div className="shell">
      <header>
        <a className="brand" href="https://usefused.com">
          harnest<span>by Fused</span>
        </a>
        <span className="integration">× CopilotKit</span>
        <span className="demo-tag">LOCAL DEMO</span>
      </header>
      <main>
        <div className="intro">
          <div>
            <p className="eyebrow">ONE AGENT. TWO FRAMEWORKS. A REAL UI.</p>
            <h1>Meet in the middle.</h1>
            <p className="lede">
              A Harnest agent, a CopilotKit conversation, and AG-UI connecting
              the two.
            </p>
          </div>
          <div className="controls">
            <label>
              Agent framework
              <select
                aria-label="Agent framework"
                value={framework}
                onChange={(e) => {
                  agents.default.abortRun();
                  setFramework(e.target.value as Framework);
                  setThread(crypto.randomUUID());
                  setError("");
                }}
              >
                <option value="adk">Google ADK</option>
                <option value="langgraph">LangGraph</option>
              </select>
            </label>
            <button
              className="secondary"
              onClick={() => {
                agents.default.abortRun();
                setThread(crypto.randomUUID());
                setError("");
              }}
            >
              New conversation
            </button>
          </div>
        </div>
        <CopilotKitProvider
          key={`${framework}:${thread}`}
          selfManagedAgents={agents}
          enableInspector={false}
          onError={({ error }) => setError(error.message)}
        >
          <Demo
            framework={framework}
            thread={thread}
            error={error}
            clearError={() => setError("")}
          />
        </CopilotKitProvider>
      </main>
      <footer>
        <span>Built and maintained by Fused</span>
        <span>
          Deterministic demo · No model key required · In-memory sessions
        </span>
      </footer>
    </div>
  );
}

function Demo({
  framework,
  thread,
  error,
  clearError,
}: {
  framework: Framework;
  thread: string;
  error: string;
  clearError: () => void;
}) {
  const { agent } = useAgent();
  const { copilotkit } = useCopilotKit();
  const [accent, setAccent] = useState("green");
  const [toolCalls, setToolCalls] = useState(0);
  const [events, setEvents] = useState<string[]>([]);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function check() {
      try {
        const response = await fetch(`/api/${framework}/healthz`, {
          signal: controller.signal,
        });
        if (response.ok) {
          setReady(true);
          return;
        }
      } catch {
        /* Compilation may still be starting the local server. */
      }
      if (!controller.signal.aborted) timer = setTimeout(check, 1500);
    }
    void check();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [framework]);
  useEffect(
    () =>
      agent.subscribe({
        onRunStartedEvent: () => clearError(),
        onEvent: ({ event }) =>
          setEvents((current) => [...current.slice(-39), event.type]),
      }).unsubscribe,
    [agent],
  );
  useFrontendTool({
    name: "set_accent",
    description: "Apply the local demo accent.",
    parameters: z.object({ color: z.enum(["violet"]) }),
    handler: async ({ color }) => {
      setAccent(color);
      setToolCalls((count) => count + 1);
      return { color };
    },
  });
  useInterrupt({
    render: ({ interrupt, resolve }) => (
      <div className="approval" role="group" aria-label="Publication approval">
        <strong>Your decision</strong>
        <p>{interrupt?.message}</p>
        <div>
          <button onClick={() => resolve({ approved: true })}>
            Approve publication
          </button>
          <button
            className="secondary"
            onClick={() => resolve({ approved: false })}
          >
            Decline publication
          </button>
        </div>
      </div>
    ),
  });

  async function send(content: string) {
    clearError();
    agent.addMessage({ id: crypto.randomUUID(), role: "user", content });
    await copilotkit.runAgent({ agent }).catch(() => {}); // Provider onError owns the visible error.
  }

  return (
    <div className={`workspace accent-${accent}`}>
      <aside>
        <div className="section-heading">
          <h2>Try the connection</h2>
          <span className={`status ${ready ? "online" : ""}`}>
            {ready ? "Backend ready" : "Connecting"}
          </span>
        </div>
        <div className="scenarios">
          {scenarios.map(([command, number, title, description]) => (
            <button
              key={command}
              className="scenario"
              disabled={!ready || agent.isRunning}
              onClick={() => void send(command)}
            >
              <span className="number">{number}</span>
              <span>
                <strong>{title}</strong>
                <small>{description}</small>
              </span>
              <span className="arrow">↗</span>
            </button>
          ))}
        </div>
        <section className="state-card">
          <p className="eyebrow">SHARED WITH THE AGENT</p>
          <div className="metrics">
            <div>
              <strong data-testid="turns">
                {String(agent.state.turns ?? 0)}
              </strong>
              <span>User turns</span>
            </div>
            <div>
              <strong data-testid="tool-calls">{toolCalls}</strong>
              <span>Browser calls</span>
            </div>
          </div>
          <div className="state-row">
            <span>Draft</span>
            <b data-testid="draft">
              {agent.state.published ? "Published" : "Not published"}
            </b>
          </div>
          <div className="state-row">
            <span>Page accent</span>
            <b data-testid="accent">{accent}</b>
          </div>
        </section>
        <details className="trace">
          <summary>
            Inspect AG-UI events <span>{events.length}</span>
          </summary>
          <ol>
            {events.map((event, index) => (
              <li key={index}>{event}</li>
            ))}
          </ol>
        </details>
        <p className="hint">
          Type <code>slow</code> to try stopping a run. New conversations start
          with fresh agent state.
        </p>
      </aside>
      <section className="conversation">
        <div className="chat-heading">
          <div>
            <span className="avatar">h</span>
            <div>
              <h2>Harnest demo agent</h2>
              <p>
                {framework === "adk" ? "Google ADK" : "LangGraph"} ·{" "}
                {agent.isRunning ? "Working…" : "Ready to chat"}
              </p>
            </div>
          </div>
          <span className="thread" title={thread}>
            Session {thread.slice(0, 8)}
          </span>
          {agent.isRunning && (
            <button className="secondary" onClick={() => agent.abortRun()}>
              Stop run
            </button>
          )}
        </div>
        {error && (
          <div className="error" role="alert">
            The run failed. Send another message to try again.
            <button aria-label="Dismiss error" onClick={clearError}>
              ×
            </button>
          </div>
        )}
        <CopilotChat
          className="chat"
          threadId={thread}
          labels={{
            welcomeMessageText: "Let’s test the connection.",
            chatInputPlaceholder: "Message the Harnest agent…",
            chatDisclaimerText:
              "Local compatibility demo. Responses are deterministic.",
          }}
        />
      </section>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<Workspace />);
