/** hello-agent (TypeScript): a one-node LangGraph graph. No LLM. Mirrors examples/hello-agent in Python. */

import { AIMessage } from "@langchain/core/messages";
import { END, MessagesAnnotation, START, StateGraph } from "@langchain/langgraph";
// In your own project: import { EnterpriseAgentApp } from "@enterprise/agent-sdk";
import { EnterpriseAgentApp } from "../../../src/index.js";

const greet = (state: typeof MessagesAnnotation.State) => {
  const prompt = state.messages[state.messages.length - 1].content;
  return { messages: [new AIMessage(`Hello from hello-agent! You said: ${prompt}`)] };
};

export function buildGraph() {
  return new StateGraph(MessagesAnnotation)
    .addNode("greet", greet)
    .addEdge(START, "greet")
    .addEdge("greet", END)
    .compile();
}

export const app = new EnterpriseAgentApp(buildGraph(), { name: "hello-agent", version: "0.1.0" });

if (process.argv[1]?.replaceAll("\\", "/").endsWith("hello-agent/src/index.js")) {
  await app.run();
}
