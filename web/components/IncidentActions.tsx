"use client";

import { useState } from "react";
import { api } from "@/lib/api";

type Props = {
  reportId: string;
  approveEnabled: boolean;
  approveLabel: string;
};

export function IncidentActions({ reportId, approveEnabled, approveLabel }: Props) {
  const [status, setStatus] = useState<"idle" | "submitting" | "done" | "error">("idle");
  const [message, setMessage] = useState<string>("");

  async function send(action: "approve" | "thumbs_up" | "thumbs_down" | "wrong_root_cause") {
    setStatus("submitting");
    try {
      if (action === "approve") {
        await api.approveAction(reportId);
        setMessage("Approved — action dispatched.");
      } else {
        await api.feedback(reportId, action);
        setMessage(`Feedback recorded (${action.replace("_", " ")}).`);
      }
      setStatus("done");
    } catch (e) {
      setStatus("error");
      setMessage(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-stretch gap-2">
        <button
          type="button"
          disabled={!approveEnabled || status === "submitting"}
          onClick={() => send("approve")}
          className="rounded-md bg-acc px-5 py-2 text-sm font-semibold text-ink-9 transition-colors duration-fast hover:bg-acc-hi disabled:cursor-not-allowed disabled:bg-ink-7 disabled:text-ink-4"
        >
          {approveLabel}
        </button>
        <button
          type="button"
          disabled={status === "submitting"}
          onClick={() => send("thumbs_up")}
          className="rounded-md border border-ink-7 px-4 py-2 text-sm text-ink-1 transition-colors duration-fast hover:border-ink-5 hover:bg-ink-8"
        >
          Thumbs up
        </button>
        <button
          type="button"
          disabled={status === "submitting"}
          onClick={() => send("wrong_root_cause")}
          className="rounded-md border border-ink-7 px-4 py-2 text-sm text-ink-3 transition-colors duration-fast hover:border-ink-5 hover:bg-ink-8"
        >
          Wrong root cause
        </button>
      </div>
      {status !== "idle" && (
        <p className={`text-xs ${status === "error" ? "text-rose-400" : "text-ink-3"}`}>{message}</p>
      )}
    </div>
  );
}
