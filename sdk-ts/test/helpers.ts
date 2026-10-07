import type { AddressInfo } from "node:net";
import { AIMessage } from "@langchain/core/messages";
import { END, MessagesAnnotation, START, StateGraph } from "@langchain/langgraph";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { EnterpriseAgentApp, type EnterpriseAgentAppOptions } from "../src/index.js";
import * as audit from "../src/audit.js";

export type Node = (state: typeof MessagesAnnotation.State, config?: any) => Promise<any> | any;

export function graphOf(node: Node) {
  return new StateGraph(MessagesAnnotation).addNode("node", node as any).addEdge(START, "node").addEdge("node", END).compile();
}

export const echo: Node = (state) => ({ messages: [new AIMessage(`echo: ${state.messages.at(-1)!.content}`)] });
export const failing: Node = () => {
  throw new RangeError("boom secret detail");
};

export function memoryProvider() {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  return { exporter, provider };
}

export interface Running {
  app: EnterpriseAgentApp;
  url: string;
  exporter: InMemorySpanExporter;
  close: () => Promise<void>;
}

export async function start(node: Node, options: Partial<EnterpriseAgentAppOptions> = {}): Promise<Running> {
  const { exporter, provider } = memoryProvider();
  const app = new EnterpriseAgentApp(graphOf(node), { name: "test-agent", version: "1.2.3", tracerProvider: provider, ...options });
  const server = await app.listen(0, "127.0.0.1");
  const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  return { app, url, exporter, close: () => app.shutdown() };
}

export async function post(url: string, body: unknown, headers: Record<string, string> = {}) {
  const res = await fetch(`${url}/invocations`, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  return { status: res.status, headers: res.headers, json: (await res.json()) as any };
}

/** Collect audit events for the duration of a test (and keep them off stdout). */
export function captureAudit(): { events: audit.AuditEvent[]; stop: () => void } {
  audit.reset();
  audit.configure(process.platform === "win32" ? "NUL" : "/dev/null");
  const events: audit.AuditEvent[] = [];
  const sink = (e: audit.AuditEvent) => events.push(e);
  audit.addSink(sink);
  return { events, stop: () => audit.reset() };
}
