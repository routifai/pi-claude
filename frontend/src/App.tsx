import { useState, useEffect } from "react";
import "./App.css";

interface Message {
  role: "user" | "assistant";
  text: string;
  harness?: string | null;
}

interface Session {
  id: string;
  messages: Message[];
  active_harness: "claude" | "pi";
  created_at: string;
}

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [activeHarness, setActiveHarness] = useState<"claude" | "pi">("claude");
  const [inputText, setInputText] = useState("");
  const [loading, setLoading] = useState(false);

  const createSession = async () => {
    const res = await fetch("/api/sessions", { method: "POST" });
    const data = await res.json();
    setSessionId(data.session_id);
    setMessages([]);
    setActiveHarness("claude");
  };

  const sendMessage = async () => {
    if (!sessionId || !inputText.trim()) return;

    setLoading(true);
    try {
      const res = await fetch(`/api/sessions/${sessionId}/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: inputText }),
      });
      const data = await res.json();

      setMessages((prev) => [
        ...prev,
        { role: "user", text: inputText },
        { role: "assistant", text: data.reply, harness: data.harness },
      ]);
      setInputText("");
    } finally {
      setLoading(false);
    }
  };

  const switchHarness = async (harness: "claude" | "pi") => {
    if (!sessionId) return;
    setActiveHarness(harness);
    await fetch(`/api/sessions/${sessionId}/switch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ harness }),
    });
  };

  return (
    <div className="container">
      <header>
        <h1>Milhe Harness Prototype</h1>
        <p>Multi-harness chat with in-session switching</p>
      </header>

      {!sessionId ? (
        <div className="start-screen">
          <button onClick={createSession} className="btn-primary">
            New Session
          </button>
        </div>
      ) : (
        <>
          <div className="controls">
            <div className="harness-picker">
              <label>Active Harness:</label>
              <button
                className={`harness-btn ${activeHarness === "claude" ? "active" : ""}`}
                onClick={() => switchHarness("claude")}
              >
                Claude
              </button>
              <button
                className={`harness-btn ${activeHarness === "pi" ? "active" : ""}`}
                onClick={() => switchHarness("pi")}
              >
                Pi
              </button>
            </div>
          </div>

          <div className="messages">
            {messages.map((msg, idx) => (
              <div key={idx} className={`message message-${msg.role}`}>
                <div className="message-header">
                  <span className="role">{msg.role}</span>
                  {msg.harness && <span className="harness">{msg.harness}</span>}
                </div>
                <div className="text">{msg.text}</div>
              </div>
            ))}
            {loading && <div className="message message-loading">Thinking...</div>}
          </div>

          <div className="input-area">
            <input
              type="text"
              value={inputText}
              onChange={(e) => setInputText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && sendMessage()}
              placeholder="Type a message..."
              disabled={loading}
            />
            <button onClick={sendMessage} disabled={loading} className="btn-send">
              Send
            </button>
            <button onClick={createSession} className="btn-new">
              New Session
            </button>
          </div>
        </>
      )}
    </div>
  );
}
