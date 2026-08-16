import { useState } from "react";
import toast from "react-hot-toast";
import { useCreateDriver } from "@/api/drivers";
import { recipePackageFile, useDraftRecipe, useRefineRecipeDraft } from "@/api/recipes";
import { Modal } from "@/components/ui/Modal";
import type { RecipeDraftResponse, RecipeValidationReport } from "@/types/ai.types";
import { errorDetail } from "@/lib/errors";

export function RecipeDraftPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [prompt, setPrompt] = useState("");
  const [hypervisorType, setHypervisorType] = useState("");
  const [draft, setDraft] = useState<RecipeDraftResponse | null>(null);
  const [feedback, setFeedback] = useState("");
  const [uploadName, setUploadName] = useState("");
  const [uploadDesc, setUploadDesc] = useState("");

  const draftRecipe = useDraftRecipe();
  const refineDraft = useRefineRecipeDraft();
  const createDriver = useCreateDriver();

  const busy = draftRecipe.isPending || refineDraft.isPending;

  const applyDraft = (d: RecipeDraftResponse) => {
    setDraft(d);
    setFeedback("");
    const metaName = typeof d.driver_metadata.name === "string" ? d.driver_metadata.name : "";
    setUploadName((prev) => prev || metaName);
  };

  const handleDraft = async () => {
    if (!prompt.trim()) {
      toast.error("Describe the recipe first");
      return;
    }
    try {
      applyDraft(
        await draftRecipe.mutateAsync({
          prompt: prompt.trim(),
          hypervisor_type: hypervisorType.trim() || undefined,
        }),
      );
    } catch (err) {
      toast.error(errorDetail(err, "Failed to draft recipe"));
    }
  };

  const handleRefine = async () => {
    if (!draft) return;
    if (!feedback.trim()) {
      toast.error("Describe what to change first");
      return;
    }
    try {
      applyDraft(
        await refineDraft.mutateAsync({ draft_id: draft.draft_id, feedback: feedback.trim() }),
      );
    } catch (err) {
      toast.error(errorDetail(err, "Failed to refine draft"));
    }
  };

  const handleApprove = async () => {
    if (!draft) return;
    if (!uploadName.trim()) {
      toast.error("Name is required");
      return;
    }
    try {
      await createDriver.mutateAsync({
        name: uploadName.trim(),
        description: uploadDesc.trim() || undefined,
        connection_type: "Hypervisor",
        file: recipePackageFile(draft.package_b64, uploadName),
      });
      toast.success("Recipe uploaded as a driver");
      handleClose();
    } catch (err) {
      toast.error(errorDetail(err, "Failed to upload recipe"));
    }
  };

  const handleDownload = () => {
    if (!draft) return;
    const file = recipePackageFile(draft.package_b64, uploadName || "generated-recipe");
    const url = URL.createObjectURL(file);
    const a = document.createElement("a");
    a.href = url;
    a.download = file.name;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleClose = () => {
    setPrompt("");
    setHypervisorType("");
    setDraft(null);
    setFeedback("");
    setUploadName("");
    setUploadDesc("");
    onClose();
  };

  return (
    <Modal open={open} onClose={handleClose} title="Draft Recipe with AI" className="max-w-4xl">
      <div className="space-y-4">
        <p className="text-xs text-gray-500">
          The AI drafts a Hypervisor recipe package; it is validated in the execution sandbox
          (structure, policy, simulated dry-run) before you see it. Nothing runs against real
          infrastructure and nothing is uploaded until you approve.
        </p>

        <div>
          <label htmlFor="recipe-prompt" className="block text-sm font-medium text-gray-700 mb-1">
            What should the recipe do?
          </label>
          <textarea
            id="recipe-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={3}
            placeholder="Example: clone a Proxmox template VM, start it, and report its management IP in field_data"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        <div className="flex items-end gap-3">
          <div className="flex-1">
            <label
              htmlFor="recipe-hypervisor-type"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Hypervisor type (optional)
            </label>
            <input
              id="recipe-hypervisor-type"
              type="text"
              value={hypervisorType}
              onChange={(e) => setHypervisorType(e.target.value)}
              placeholder="proxmox, vsphere, libvirt"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <button
            type="button"
            onClick={handleDraft}
            disabled={busy}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            {draftRecipe.isPending ? "Drafting and validating..." : draft ? "Redraft" : "Draft"}
          </button>
        </div>

        {draft && (
          <div className="space-y-4 border-t border-gray-200 pt-4">
            <ValidationSummary report={draft.validation} attempts={draft.attempts} />

            {draft.explanation && (
              <p className="text-sm text-gray-700 bg-gray-50 border border-gray-200 rounded-lg p-3">
                {draft.explanation}
              </p>
            )}

            <details open>
              <summary className="text-sm font-medium text-gray-900 cursor-pointer">
                driver.py
              </summary>
              <pre className="mt-2 text-xs font-mono bg-gray-900 text-gray-100 rounded-lg p-3 overflow-x-auto max-h-96 overflow-y-auto whitespace-pre">
                {draft.driver_py}
              </pre>
            </details>
            <details>
              <summary className="text-sm font-medium text-gray-900 cursor-pointer">
                driver_metadata.json
              </summary>
              <pre className="mt-2 text-xs font-mono bg-gray-50 border border-gray-200 rounded-lg p-3 overflow-x-auto">
                {JSON.stringify(draft.driver_metadata, null, 2)}
              </pre>
            </details>

            <div className="flex items-end gap-3">
              <div className="flex-1">
                <label
                  htmlFor="recipe-feedback"
                  className="block text-sm font-medium text-gray-700 mb-1"
                >
                  Request changes
                </label>
                <textarea
                  id="recipe-feedback"
                  value={feedback}
                  onChange={(e) => setFeedback(e.target.value)}
                  rows={2}
                  placeholder="Example: verify TLS against the hypervisor CA and add a node parameter"
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <button
                type="button"
                onClick={handleRefine}
                disabled={busy}
                className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 disabled:opacity-50 transition-colors"
              >
                {refineDraft.isPending ? "Refining..." : "Refine"}
              </button>
            </div>

            <div className="border-t border-gray-200 pt-4 space-y-3">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label
                    htmlFor="recipe-upload-name"
                    className="block text-sm font-medium text-gray-700 mb-1"
                  >
                    Driver name
                  </label>
                  <input
                    id="recipe-upload-name"
                    type="text"
                    value={uploadName}
                    onChange={(e) => setUploadName(e.target.value)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
                <div>
                  <label
                    htmlFor="recipe-upload-desc"
                    className="block text-sm font-medium text-gray-700 mb-1"
                  >
                    Description
                  </label>
                  <input
                    id="recipe-upload-desc"
                    type="text"
                    value={uploadDesc}
                    onChange={(e) => setUploadDesc(e.target.value)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
              </div>
              {!draft.valid && (
                <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-2">
                  This draft failed validation, so it cannot be uploaded from here. Refine it
                  until it passes, or download the package and finish it by hand through the
                  normal upload form.
                </p>
              )}
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={handleDownload}
                  className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
                >
                  Download package
                </button>
                <button
                  type="button"
                  onClick={handleApprove}
                  disabled={!draft.valid || createDriver.isPending || busy}
                  className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
                >
                  {createDriver.isPending ? "Uploading..." : "Approve and upload"}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}

function ValidationSummary({
  report,
  attempts,
}: {
  report: RecipeValidationReport | null;
  attempts: number;
}) {
  if (!report) {
    return (
      <p className="text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded-lg p-3">
        No validation report is attached to this draft.
      </p>
    );
  }
  const banner = report.valid
    ? "bg-green-50 border-green-200 text-green-800"
    : "bg-amber-50 border-amber-200 text-amber-800";
  const failures = [
    ...report.structural.errors.map((e) => `structural: ${e}`),
    ...report.policy.errors.map((e) => `policy: ${e}`),
  ];
  return (
    <div className={`border rounded-lg p-3 text-sm ${banner}`}>
      <div className="font-medium">
        {report.valid
          ? `Validation passed (${attempts} attempt${attempts === 1 ? "" : "s"})`
          : `Validation failed after ${attempts} attempt${attempts === 1 ? "" : "s"}`}
      </div>
      {failures.length > 0 && (
        <ul className="mt-1 list-disc pl-5 text-xs">
          {failures.map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      )}
      {report.dry_run.methods.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs font-medium">
            Simulated dry-run: {report.dry_run.methods.filter((m) => m.passed).length}/
            {report.dry_run.methods.length} lifecycle methods passed
          </summary>
          <ul className="mt-1 space-y-1 text-xs">
            {report.dry_run.methods.map((m) => (
              <li key={m.action}>
                <span className="font-mono">{m.action}</span>: {m.passed ? "passed" : "FAILED"}
                {m.error ? `, ${m.error}` : ""}
                {m.transcript.length > 0 && (
                  <details className="ml-4">
                    <summary className="cursor-pointer">transcript</summary>
                    <pre className="mt-1 font-mono bg-white/60 rounded p-2 overflow-x-auto">
                      {JSON.stringify(m.transcript, null, 2)}
                    </pre>
                  </details>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
