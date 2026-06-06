import { useRef, useState } from "react";
import toast from "react-hot-toast";
import { isAxiosError } from "axios";

import { Modal } from "@/components/ui/Modal";
import { useAIGenerate } from "@/api/ai";
import type { AIGenerateResponse } from "@/types/ai.types";

interface AIDialogProps {
  open: boolean;
  onClose: () => void;
  onProposal: (response: AIGenerateResponse) => void;
}

const ACCEPTED_EXTENSIONS = [".pdf", ".txt", ".md", ".json", ".xml", ".tgz", ".tar.gz"];
const MAX_FILES = 5;
const MAX_FILE_BYTES = 5 * 1024 * 1024;

function hasAcceptedExtension(name: string): boolean {
  const lower = name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

export function AIDialog({ open, onClose, onProposal }: AIDialogProps) {
  const [prompt, setPrompt] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const generate = useAIGenerate();

  const reset = () => {
    setPrompt("");
    setFiles([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const handleFilesPicked = (picked: FileList | null) => {
    if (!picked) return;
    const next: File[] = [...files];
    for (const f of Array.from(picked)) {
      if (!hasAcceptedExtension(f.name)) {
        toast.error(`Unsupported file type: ${f.name}`);
        continue;
      }
      if (f.size > MAX_FILE_BYTES) {
        toast.error(`${f.name} exceeds 5 MB limit`);
        continue;
      }
      if (next.length >= MAX_FILES) {
        toast.error(`Limit is ${MAX_FILES} files per request`);
        break;
      }
      if (!next.some((existing) => existing.name === f.name && existing.size === f.size)) {
        next.push(f);
      }
    }
    setFiles(next);
  };

  const removeFile = (idx: number) => {
    setFiles(files.filter((_, i) => i !== idx));
  };

  const handleSubmit = async () => {
    if (!prompt.trim()) {
      toast.error("Enter a prompt before generating");
      return;
    }
    try {
      const result = await generate.mutateAsync({ prompt: prompt.trim(), files });
      onProposal(result);
      reset();
      onClose();
    } catch (err) {
      if (isAxiosError(err)) {
        if (err.response?.status === 503) {
          toast.error("AI feature is not configured: ANTHROPIC_API_KEY is blank");
        } else if (err.response?.status === 400) {
          const detail = err.response.data?.detail ?? "Invalid upload";
          toast.error(`Upload rejected: ${detail}`);
        } else if (err.response?.status === 502) {
          const detail = err.response.data?.detail ?? "Upstream error";
          toast.error(`AI proposal rejected: ${detail}`);
        } else {
          toast.error("Failed to generate topology");
        }
      } else {
        toast.error("Failed to generate topology");
      }
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Generate topology with AI" className="max-w-2xl">
      <div className="space-y-4">
        <p className="text-sm text-gray-600">
          Describe the topology you want. The AI will pick available devices from
          your inventory and return a proposal you can review before committing
          to the canvas.
        </p>
        <div>
          <label htmlFor="ai-prompt" className="block text-sm font-medium text-gray-700 mb-1">
            Prompt
          </label>
          <textarea
            id="ai-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={6}
            placeholder="e.g. Set up a two-firewall HA pair with a client behind it"
            className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        <div>
          <label htmlFor="ai-files" className="block text-sm font-medium text-gray-700 mb-1">
            Reference files (optional)
          </label>
          <input
            id="ai-files"
            ref={fileInputRef}
            type="file"
            multiple
            accept={ACCEPTED_EXTENSIONS.join(",")}
            onChange={(e) => handleFilesPicked(e.target.files)}
            className="block w-full text-sm text-gray-700"
          />
          <p className="text-xs text-gray-500 mt-1">
            Accepted: {ACCEPTED_EXTENSIONS.join(", ")}. Up to {MAX_FILES} files, 5 MB each.
          </p>
          {files.length > 0 && (
            <ul className="mt-2 space-y-1">
              {files.map((f, idx) => (
                <li
                  key={`${f.name}-${idx}`}
                  className="flex items-center justify-between text-sm bg-gray-50 px-2 py-1 rounded"
                >
                  <span className="truncate">
                    {f.name} ({Math.ceil(f.size / 1024)} KB)
                  </span>
                  <button
                    type="button"
                    onClick={() => removeFile(idx)}
                    className="text-xs text-red-600 hover:text-red-800 ml-2"
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            disabled={generate.isPending}
            className="px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 rounded-md disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={generate.isPending || !prompt.trim()}
            className="px-4 py-2 text-sm bg-blue-600 text-white hover:bg-blue-700 rounded-md disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {generate.isPending ? "Generating..." : "Generate"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
