import type {
  ArrInstance,
  InstanceCreateInput,
  InstanceUpdateInput,
  PathMapping,
  PathMappingCreateInput,
  PathMappingUpdateInput,
} from "../types/instances";
import { apiErrorMessage, apiFetch } from "./client";

const JSON_HEADERS = { "Content-Type": "application/json" };

/** Lists all configured Sonarr/Radarr instances (`GET /api/instances`, COL-27). */
export async function fetchInstances(): Promise<ArrInstance[]> {
  const response = await apiFetch("/api/instances");
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load instances (${response.status})`));
  }
  return (await response.json()) as ArrInstance[];
}

/** Creates an arr instance (`POST /api/instances`, COL-27). */
export async function createInstance(input: InstanceCreateInput): Promise<ArrInstance> {
  const response = await apiFetch("/api/instances", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to create instance (${response.status})`));
  }
  return (await response.json()) as ArrInstance;
}

/** Updates an arr instance's editable fields (`PUT /api/instances/{id}`, COL-27). */
export async function updateInstance(id: number, input: InstanceUpdateInput): Promise<ArrInstance> {
  const response = await apiFetch(`/api/instances/${id}`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to update instance (${response.status})`));
  }
  return (await response.json()) as ArrInstance;
}

/** Deletes an arr instance, cascading to its path mappings (`DELETE /api/instances/{id}`, COL-27). */
export async function deleteInstance(id: number): Promise<void> {
  const response = await apiFetch(`/api/instances/${id}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to delete instance (${response.status})`));
  }
}

/** Lists an instance's path mappings in application order (COL-27). */
export async function fetchPathMappings(instanceId: number): Promise<PathMapping[]> {
  const response = await apiFetch(`/api/instances/${instanceId}/path-mappings`);
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load path mappings (${response.status})`));
  }
  return (await response.json()) as PathMapping[];
}

/** Creates a path mapping under an instance (COL-27). */
export async function createPathMapping(
  instanceId: number,
  input: PathMappingCreateInput,
): Promise<PathMapping> {
  const response = await apiFetch(`/api/instances/${instanceId}/path-mappings`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to create path mapping (${response.status})`));
  }
  return (await response.json()) as PathMapping;
}

/** Updates a path mapping's fields (COL-27). */
export async function updatePathMapping(
  instanceId: number,
  mappingId: number,
  input: PathMappingUpdateInput,
): Promise<PathMapping> {
  const response = await apiFetch(`/api/instances/${instanceId}/path-mappings/${mappingId}`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to update path mapping (${response.status})`));
  }
  return (await response.json()) as PathMapping;
}

/** Deletes a path mapping (COL-27). */
export async function deletePathMapping(instanceId: number, mappingId: number): Promise<void> {
  const response = await apiFetch(`/api/instances/${instanceId}/path-mappings/${mappingId}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to delete path mapping (${response.status})`));
  }
}
