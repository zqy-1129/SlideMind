export interface Dataset {
  id: string
  name: string
  description?: string
  created_at: string
}

export interface ImportTask {
  id: string
  file_id: string
  dataset_id: string
  status: string
  data_type: string
  gis_category?: string
  logs: string[]
  error_rows: Record<string, unknown>[]
  created_at: string
  updated_at: string
}

export interface GraphNode {
  id: string
  label: string
  type: string
  properties: Record<string, unknown>
}

export interface GraphEdge {
  id: string
  source: string
  target: string
  label: string
  properties: Record<string, unknown>
}

export interface GraphBuildTask {
  task_id: string
  status: string
  message: string
}

export interface GraphTask {
  id: string
  dataset_id: string
  status: string
  progress: number
  logs: string[]
  summary: Record<string, unknown>
  error?: string
  created_at: string
  updated_at: string
}

export interface Answer {
  answer: string
  route: string
  sources: Record<string, unknown>[]
}

export interface TabularRecord {
  id: string
  dataset_id: string
  source_file_id: string
  row_number: number
  data_type: string
  timestamp?: string
  location?: Record<string, unknown>
  raw_fields: Record<string, unknown>
  normalized_fields: Record<string, unknown>
  created_at: string
}

export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface DocumentItem {
  id: string
  dataset_id: string
  source_file_id: string
  title?: string
  created_at: string
}

export interface DocumentChunk {
  id: string
  dataset_id: string
  source_file_id: string
  document_id: string
  chunk_index: number
  text: string
  tuple_ids?: string[]
  region_id?: string
  region_name?: string
  region_match_method?: string
  region_confidence?: number
  extraction_status?: string
  milvus_vector_id?: string
  created_at: string
}

export interface GisFeature {
  id: string
  dataset_id: string
  source_file_id: string
  feature_index: number
  data_type: string
  gis_category?: string
  gis_category_name?: string
  layer_name?: string
  geometry_type?: string
  properties: Record<string, unknown>
  geometry: Record<string, unknown>
  bbox?: number[]
  centroid?: Record<string, number>
  created_at: string
}

const API_BASE = '/api'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, options)
  if (!response.ok) {
    const message = await response.text()
    throw new Error(message || `Request failed: ${response.status}`)
  }
  return response.json() as Promise<T>
}

export const api = {
  listDatasets: () => request<Dataset[]>('/datasets'),
  createDataset: (payload: { name: string; description?: string }) =>
    request<Dataset>('/datasets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }),
  deleteDataset: (datasetId: string) =>
    request<Record<string, unknown>>(`/datasets/${datasetId}`, {
      method: 'DELETE'
    }),
  uploadFile: (datasetId: string, dataType: string, file: File, gisCategory?: string) => {
    const formData = new FormData()
    formData.append('dataset_id', datasetId)
    formData.append('data_type', dataType)
    if (gisCategory) formData.append('gis_category', gisCategory)
    formData.append('file', file)
    return request<{ task_id: string; file_id: string; status: string; message: string }>('/imports', {
      method: 'POST',
      body: formData
    })
  },
  listImports: (datasetId?: string) =>
    request<ImportTask[]>(datasetId ? `/imports?dataset_id=${datasetId}` : '/imports'),
  retryImport: (taskId: string) =>
    request<{ task_id: string; file_id: string; status: string; message: string }>(`/imports/${taskId}/retry`, {
      method: 'POST'
    }),
  deleteImport: (taskId: string) =>
    request<Record<string, unknown>>(`/imports/${taskId}`, {
      method: 'DELETE'
    }),
  deleteData: (datasetId: string, dataKind: string) =>
    request<Record<string, unknown>>(`/data?dataset_id=${datasetId}&data_kind=${dataKind}`, {
      method: 'DELETE'
    }),
  listRecords: (datasetId?: string, dataType?: string, page = 1, pageSize = 20) => {
    const params = new URLSearchParams()
    if (datasetId) params.set('dataset_id', datasetId)
    if (dataType) params.set('data_type', dataType)
    params.set('page', String(page))
    params.set('page_size', String(pageSize))
    const query = params.toString()
    return request<PageResult<TabularRecord>>(query ? `/records?${query}` : '/records')
  },
  listDocuments: (datasetId?: string) =>
    request<DocumentItem[]>(datasetId ? `/documents?dataset_id=${datasetId}&limit=1000` : '/documents?limit=1000'),
  listDocumentChunks: (datasetId?: string) =>
    request<DocumentChunk[]>(datasetId ? `/document-chunks?dataset_id=${datasetId}&limit=1000` : '/document-chunks?limit=1000'),
  listGisFeatures: (datasetId?: string, page = 1, pageSize = 20) => {
    const params = new URLSearchParams()
    if (datasetId) params.set('dataset_id', datasetId)
    params.set('page', String(page))
    params.set('page_size', String(pageSize))
    return request<PageResult<GisFeature>>(`/gis-features?${params.toString()}`)
  },
  buildGraph: (datasetId: string, includeTextKg = true) =>
    request<GraphBuildTask>('/graph/build', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dataset_id: datasetId, include_text_kg: includeTextKg })
    }),
  getGraphTask: (taskId: string) => request<GraphTask>(`/graph/tasks/${taskId}`),
  getGraph: (datasetId?: string, limit = 20, nodeType?: string, parentId?: string) => {
    const params = new URLSearchParams()
    if (datasetId) params.set('dataset_id', datasetId)
    params.set('limit', String(limit))
    if (nodeType) params.set('node_type', nodeType)
    if (parentId) params.set('parent_id', parentId)
    return request<{ nodes: GraphNode[]; edges: GraphEdge[] }>(`/graph?${params.toString()}`)
  },
  getGraphNodeTypes: (datasetId?: string) =>
    request<{ types: string[] }>(datasetId ? `/graph/node-types?dataset_id=${datasetId}` : '/graph/node-types'),
  ask: (question: string, datasetId?: string) =>
    request<Answer>('/qa', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, dataset_id: datasetId || null })
    })
}
