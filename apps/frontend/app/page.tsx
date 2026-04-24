"use client";
import { useState } from "react";
import { api } from "@/lib/api";

export default function Home() {
  const [response, setResponse] = useState<string>("");
  const [loading, setLoading] = useState(false);

  const ping = async () => {
    setLoading(true);
    try {
      const res = await api.get("/health/echo?message=converio");
      setResponse(JSON.stringify(res.data, null, 2));
    } catch (err) {
      setResponse(String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="p-8 space-y-4">
      <h1 className="text-2xl font-bold">Converio</h1>
      <button
        onClick={ping}
        disabled={loading}
        className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
      >
        {loading ? "Pinging..." : "Ping API"}
      </button>
      {response && (
        <pre className="bg-gray-100 p-4 rounded text-sm overflow-auto">
          {response}
        </pre>
      )}
    </div>
  );
}
