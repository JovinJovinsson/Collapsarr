import { useEffect, useState } from "react";

import {
  createPathMapping,
  deletePathMapping,
  fetchPathMappings,
  updatePathMapping,
} from "../../api/instances";
import type { PathMapping } from "../../types/instances";

type ListState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; mappings: PathMapping[] };

interface MappingFormValues {
  remote_prefix: string;
  local_prefix: string;
  order: string;
}

const EMPTY_FORM: MappingFormValues = { remote_prefix: "", local_prefix: "", order: "0" };

/** Validates a mapping form; returns an error message, or `null` when valid. */
function validateMappingForm(form: MappingFormValues): string | null {
  if (!form.remote_prefix.trim()) return "Remote prefix is required.";
  if (!form.local_prefix.trim()) return "Local prefix is required.";
  if (form.order.trim() !== "" && !Number.isInteger(Number(form.order))) {
    return "Order must be a whole number.";
  }
  return null;
}

/**
 * Path-mapping CRUD for a single arr instance (COL-27's nested
 * `/api/instances/{id}/path-mappings` resource), rendered inline beneath an
 * expanded instance row in `InstancesSection`.
 */
export function PathMappingsPanel({ instanceId }: { instanceId: number }) {
  const [state, setState] = useState<ListState>({ status: "loading" });

  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState<MappingFormValues>(EMPTY_FORM);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState<MappingFormValues>(EMPTY_FORM);
  const [editError, setEditError] = useState<string | null>(null);
  const [savingEditId, setSavingEditId] = useState<number | null>(null);

  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  function reload() {
    setState({ status: "loading" });
    fetchPathMappings(instanceId)
      .then((mappings) => setState({ status: "ready", mappings }))
      .catch((error: unknown) =>
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "Unknown error.",
        }),
      );
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instanceId]);

  async function handleCreate() {
    const validationError = validateMappingForm(createForm);
    if (validationError) {
      setCreateError(validationError);
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      await createPathMapping(instanceId, {
        remote_prefix: createForm.remote_prefix.trim(),
        local_prefix: createForm.local_prefix.trim(),
        order: createForm.order.trim() === "" ? 0 : Number(createForm.order),
      });
      setCreateForm(EMPTY_FORM);
      setShowCreate(false);
      reload();
    } catch (error: unknown) {
      setCreateError(error instanceof Error ? error.message : "Unknown error.");
    } finally {
      setCreating(false);
    }
  }

  function startEdit(mapping: PathMapping) {
    setEditingId(mapping.id);
    setEditForm({
      remote_prefix: mapping.remote_prefix,
      local_prefix: mapping.local_prefix,
      order: String(mapping.order),
    });
    setEditError(null);
  }

  async function handleSaveEdit(mappingId: number) {
    const validationError = validateMappingForm(editForm);
    if (validationError) {
      setEditError(validationError);
      return;
    }
    setSavingEditId(mappingId);
    setEditError(null);
    try {
      await updatePathMapping(instanceId, mappingId, {
        remote_prefix: editForm.remote_prefix.trim(),
        local_prefix: editForm.local_prefix.trim(),
        order: Number(editForm.order),
      });
      setEditingId(null);
      reload();
    } catch (error: unknown) {
      setEditError(error instanceof Error ? error.message : "Unknown error.");
    } finally {
      setSavingEditId(null);
    }
  }

  async function handleDelete(mapping: PathMapping) {
    if (!globalThis.confirm?.(`Delete path mapping "${mapping.remote_prefix} → ${mapping.local_prefix}"?`)) {
      return;
    }
    setDeletingId(mapping.id);
    setDeleteError(null);
    try {
      await deletePathMapping(instanceId, mapping.id);
      reload();
    } catch (error: unknown) {
      setDeleteError(error instanceof Error ? error.message : "Unknown error.");
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="mappings-panel">
      <div className="mappings-panel__header">
        <h4 className="mappings-panel__title">Path mappings</h4>
        <button
          type="button"
          className="btn btn--secondary btn--sm"
          onClick={() => {
            setShowCreate((value) => !value);
            setCreateError(null);
          }}
        >
          {showCreate ? "Cancel" : "Add mapping"}
        </button>
      </div>

      {showCreate && (
        <div className="mapping-form">
          <div className="form-field">
            <label htmlFor={`remote-prefix-${instanceId}`}>Remote prefix</label>
            <input
              id={`remote-prefix-${instanceId}`}
              type="text"
              placeholder="/tv"
              value={createForm.remote_prefix}
              onChange={(event) => setCreateForm({ ...createForm, remote_prefix: event.target.value })}
            />
          </div>
          <div className="form-field">
            <label htmlFor={`local-prefix-${instanceId}`}>Local prefix</label>
            <input
              id={`local-prefix-${instanceId}`}
              type="text"
              placeholder="/mnt/media/tv"
              value={createForm.local_prefix}
              onChange={(event) => setCreateForm({ ...createForm, local_prefix: event.target.value })}
            />
          </div>
          <div className="form-field form-field--narrow">
            <label htmlFor={`order-${instanceId}`}>Order</label>
            <input
              id={`order-${instanceId}`}
              type="number"
              value={createForm.order}
              onChange={(event) => setCreateForm({ ...createForm, order: event.target.value })}
            />
          </div>
          <div className="form-actions">
            <button type="button" className="btn btn--primary btn--sm" onClick={handleCreate} disabled={creating}>
              {creating ? "Adding…" : "Add"}
            </button>
          </div>
          {createError && <p className="form-error">{createError}</p>}
        </div>
      )}

      {state.status === "loading" && <p className="mappings-panel__message">Loading path mappings…</p>}

      {state.status === "error" && (
        <p className="mappings-panel__message mappings-panel__message--error">
          Couldn&apos;t load path mappings: {state.message}
        </p>
      )}

      {state.status === "ready" && state.mappings.length === 0 && (
        <p className="mappings-panel__message">No path mappings configured for this instance.</p>
      )}

      {state.status === "ready" && state.mappings.length > 0 && (
        <table className="mappings-table">
          <thead>
            <tr>
              <th scope="col">Remote prefix</th>
              <th scope="col">Local prefix</th>
              <th scope="col">Order</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {state.mappings.map((mapping) =>
              editingId === mapping.id ? (
                <tr key={mapping.id}>
                  <td>
                    <input
                      type="text"
                      aria-label="Edit remote prefix"
                      value={editForm.remote_prefix}
                      onChange={(event) => setEditForm({ ...editForm, remote_prefix: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      type="text"
                      aria-label="Edit local prefix"
                      value={editForm.local_prefix}
                      onChange={(event) => setEditForm({ ...editForm, local_prefix: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      type="number"
                      aria-label="Edit order"
                      value={editForm.order}
                      onChange={(event) => setEditForm({ ...editForm, order: event.target.value })}
                    />
                  </td>
                  <td className="mappings-table__actions">
                    <button
                      type="button"
                      className="btn btn--primary btn--sm"
                      onClick={() => handleSaveEdit(mapping.id)}
                      disabled={savingEditId === mapping.id}
                    >
                      {savingEditId === mapping.id ? "Saving…" : "Save"}
                    </button>
                    <button type="button" className="btn btn--ghost btn--sm" onClick={() => setEditingId(null)}>
                      Cancel
                    </button>
                    {editError && <p className="form-error">{editError}</p>}
                  </td>
                </tr>
              ) : (
                <tr key={mapping.id}>
                  <td className="mappings-table__prefix">{mapping.remote_prefix}</td>
                  <td className="mappings-table__prefix">{mapping.local_prefix}</td>
                  <td>{mapping.order}</td>
                  <td className="mappings-table__actions">
                    <button type="button" className="btn btn--ghost btn--sm" onClick={() => startEdit(mapping)}>
                      Edit
                    </button>
                    <button
                      type="button"
                      className="btn btn--danger btn--sm"
                      onClick={() => handleDelete(mapping)}
                      disabled={deletingId === mapping.id}
                    >
                      {deletingId === mapping.id ? "Deleting…" : "Delete"}
                    </button>
                  </td>
                </tr>
              ),
            )}
          </tbody>
        </table>
      )}

      {deleteError && <p className="form-error">{deleteError}</p>}
    </div>
  );
}
