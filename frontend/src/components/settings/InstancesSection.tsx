import { Fragment, useEffect, useState } from "react";

import { createInstance, deleteInstance, fetchInstances, updateInstance } from "../../api/instances";
import type { ArrInstance, InstanceType } from "../../types/instances";
import { PathMappingsPanel } from "./PathMappingsPanel";

type ListState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; instances: ArrInstance[] };

interface InstanceFormValues {
  name: string;
  type: InstanceType;
  base_url: string;
  api_key: string;
}

const EMPTY_FORM: InstanceFormValues = { name: "", type: "sonarr", base_url: "", api_key: "" };

const TYPE_LABEL: Record<InstanceType, string> = { sonarr: "Sonarr", radarr: "Radarr" };
const STATUS_LABEL: Record<ArrInstance["status"], string> = {
  ok: "Connected",
  error: "Error",
  unknown: "Unknown",
};

/** Validates an instance form; returns an error message, or `null` when valid. */
function validateInstanceForm(form: InstanceFormValues): string | null {
  if (!form.name.trim()) return "Name is required.";
  if (!form.base_url.trim()) return "Base URL is required.";
  try {
    new URL(form.base_url.trim());
  } catch {
    return "Base URL must be a valid URL, e.g. http://localhost:8989.";
  }
  if (!form.api_key.trim()) return "API key is required.";
  return null;
}

/**
 * Instances + path-mappings CRUD (COL-33's AC1), backed by COL-27's
 * `/api/instances` and nested `/api/instances/{id}/path-mappings`.
 */
export function InstancesSection() {
  const [state, setState] = useState<ListState>({ status: "loading" });

  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState<InstanceFormValues>(EMPTY_FORM);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState<InstanceFormValues>(EMPTY_FORM);
  const [editError, setEditError] = useState<string | null>(null);
  const [savingEditId, setSavingEditId] = useState<number | null>(null);

  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const [expandedId, setExpandedId] = useState<number | null>(null);

  function reload() {
    setState({ status: "loading" });
    fetchInstances()
      .then((instances) => setState({ status: "ready", instances }))
      .catch((error: unknown) =>
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "Unknown error.",
        }),
      );
  }

  useEffect(() => {
    reload();
  }, []);

  async function handleCreate() {
    const validationError = validateInstanceForm(createForm);
    if (validationError) {
      setCreateError(validationError);
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      await createInstance({
        name: createForm.name.trim(),
        type: createForm.type,
        base_url: createForm.base_url.trim(),
        api_key: createForm.api_key.trim(),
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

  function startEdit(instance: ArrInstance) {
    setEditingId(instance.id);
    setEditForm({
      name: instance.name,
      type: instance.type,
      base_url: instance.base_url,
      api_key: instance.api_key,
    });
    setEditError(null);
  }

  async function handleSaveEdit(instanceId: number) {
    const validationError = validateInstanceForm(editForm);
    if (validationError) {
      setEditError(validationError);
      return;
    }
    setSavingEditId(instanceId);
    setEditError(null);
    try {
      await updateInstance(instanceId, {
        name: editForm.name.trim(),
        base_url: editForm.base_url.trim(),
        api_key: editForm.api_key.trim(),
      });
      setEditingId(null);
      reload();
    } catch (error: unknown) {
      setEditError(error instanceof Error ? error.message : "Unknown error.");
    } finally {
      setSavingEditId(null);
    }
  }

  async function handleDelete(instance: ArrInstance) {
    if (!globalThis.confirm?.(`Delete instance "${instance.name}"? This also removes its path mappings.`)) {
      return;
    }
    setDeletingId(instance.id);
    setDeleteError(null);
    try {
      await deleteInstance(instance.id);
      if (expandedId === instance.id) setExpandedId(null);
      reload();
    } catch (error: unknown) {
      setDeleteError(error instanceof Error ? error.message : "Unknown error.");
    } finally {
      setDeletingId(null);
    }
  }

  const instances = state.status === "ready" ? state.instances : [];

  return (
    <section className="settings-section">
      <div className="settings-section__header">
        <div>
          <h2 className="settings-section__title">Instances</h2>
          <p className="settings-section__summary">
            Sonarr/Radarr connections and their remote-to-local path mappings.
          </p>
        </div>
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => {
            setShowCreate((value) => !value);
            setCreateError(null);
          }}
        >
          {showCreate ? "Cancel" : "Add instance"}
        </button>
      </div>

      {showCreate && (
        <div className="panel instance-form">
          <div className="form-grid">
            <div className="form-field">
              <label htmlFor="instance-name">Name</label>
              <input
                id="instance-name"
                type="text"
                placeholder="Sonarr"
                value={createForm.name}
                onChange={(event) => setCreateForm({ ...createForm, name: event.target.value })}
              />
            </div>
            <div className="form-field">
              <label htmlFor="instance-type">Type</label>
              <select
                id="instance-type"
                value={createForm.type}
                onChange={(event) =>
                  setCreateForm({ ...createForm, type: event.target.value as InstanceType })
                }
              >
                <option value="sonarr">Sonarr</option>
                <option value="radarr">Radarr</option>
              </select>
            </div>
            <div className="form-field">
              <label htmlFor="instance-base-url">Base URL</label>
              <input
                id="instance-base-url"
                type="text"
                placeholder="http://localhost:8989"
                value={createForm.base_url}
                onChange={(event) => setCreateForm({ ...createForm, base_url: event.target.value })}
              />
            </div>
            <div className="form-field">
              <label htmlFor="instance-api-key">API key</label>
              <input
                id="instance-api-key"
                type="text"
                value={createForm.api_key}
                onChange={(event) => setCreateForm({ ...createForm, api_key: event.target.value })}
              />
            </div>
          </div>
          <div className="form-actions">
            <button type="button" className="btn btn--primary" onClick={handleCreate} disabled={creating}>
              {creating ? "Saving…" : "Save instance"}
            </button>
          </div>
          {createError && <p className="form-error">{createError}</p>}
        </div>
      )}

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading instances…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <p className="panel__message">Couldn&apos;t load instances: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && instances.length === 0 && (
        <div className="panel panel--empty">
          <p className="panel__message">
            No arr instances configured yet. Add a Sonarr or Radarr connection to get started.
          </p>
        </div>
      )}

      {instances.length > 0 && (
        <div className="panel instances-panel">
          <table className="instances-table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Type</th>
                <th scope="col">Base URL</th>
                <th scope="col">Status</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {instances.map((instance) => (
                <Fragment key={instance.id}>
                  {editingId === instance.id ? (
                    <tr>
                      <td>
                        <input
                          type="text"
                          aria-label="Edit instance name"
                          value={editForm.name}
                          onChange={(event) => setEditForm({ ...editForm, name: event.target.value })}
                        />
                      </td>
                      <td>{TYPE_LABEL[instance.type]}</td>
                      <td>
                        <input
                          type="text"
                          aria-label="Edit base URL"
                          value={editForm.base_url}
                          onChange={(event) => setEditForm({ ...editForm, base_url: event.target.value })}
                        />
                      </td>
                      <td>
                        <input
                          type="text"
                          aria-label="Edit API key"
                          value={editForm.api_key}
                          onChange={(event) => setEditForm({ ...editForm, api_key: event.target.value })}
                        />
                      </td>
                      <td className="instances-table__actions">
                        <button
                          type="button"
                          className="btn btn--primary btn--sm"
                          onClick={() => handleSaveEdit(instance.id)}
                          disabled={savingEditId === instance.id}
                        >
                          {savingEditId === instance.id ? "Saving…" : "Save"}
                        </button>
                        <button
                          type="button"
                          className="btn btn--ghost btn--sm"
                          onClick={() => setEditingId(null)}
                        >
                          Cancel
                        </button>
                        {editError && <p className="form-error">{editError}</p>}
                      </td>
                    </tr>
                  ) : (
                    <tr>
                      <td className="instances-table__name">{instance.name}</td>
                      <td>{TYPE_LABEL[instance.type]}</td>
                      <td className="instances-table__url">{instance.base_url}</td>
                      <td>
                        <span
                          className={`instances-table__status instances-table__status--${instance.status}`}
                        >
                          {STATUS_LABEL[instance.status]}
                        </span>
                      </td>
                      <td className="instances-table__actions">
                        <button
                          type="button"
                          className="btn btn--ghost btn--sm"
                          onClick={() => setExpandedId(expandedId === instance.id ? null : instance.id)}
                        >
                          {expandedId === instance.id ? "Hide mappings" : "Manage mappings"}
                        </button>
                        <button
                          type="button"
                          className="btn btn--ghost btn--sm"
                          onClick={() => startEdit(instance)}
                        >
                          Edit
                        </button>
                        <button
                          type="button"
                          className="btn btn--danger btn--sm"
                          onClick={() => handleDelete(instance)}
                          disabled={deletingId === instance.id}
                        >
                          {deletingId === instance.id ? "Deleting…" : "Delete"}
                        </button>
                      </td>
                    </tr>
                  )}
                  {expandedId === instance.id && (
                    <tr>
                      <td colSpan={5} className="instances-table__mappings-cell">
                        <PathMappingsPanel instanceId={instance.id} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {deleteError && <p className="form-error">{deleteError}</p>}
    </section>
  );
}
