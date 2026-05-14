<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  ChatDotRound,
  Connection,
  DataAnalysis,
  Delete,
  Document,
  FolderAdd,
  Location,
  Refresh,
  Tickets,
  UploadFilled
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox, type UploadInstance, type UploadUserFile } from 'element-plus'
import * as echarts from 'echarts'
import {
  api,
  type Answer,
  type Dataset,
  type DocumentChunk,
  type DocumentItem,
  type GisFeature,
  type GraphEdge,
  type GraphNode,
  type GraphTask,
  type ImportTask,
  type TabularRecord
} from './api/client'

type AppPage = 'data' | 'analysis'
type DataView = 'insar' | 'water_level' | 'rainfall' | 'gis_vector' | 'documents' | 'chunks'

const activePage = ref<AppPage>('data')
const datasets = ref<Dataset[]>([])
const imports = ref<ImportTask[]>([])
const records = ref<TabularRecord[]>([])
const recordTotal = ref(0)
const recordPage = ref(1)
const recordPageSize = ref(20)
const gisFeatures = ref<GisFeature[]>([])
const gisTotal = ref(0)
const gisPage = ref(1)
const gisPageSize = ref(20)
const documents = ref<DocumentItem[]>([])
const chunks = ref<DocumentChunk[]>([])
const selectedDatasetId = ref('')
const selectedChunkId = ref('')
const datasetName = ref('')
const datasetDescription = ref('')
const uploadDataType = ref('insar')
const uploadGisCategory = ref('area')
const dataView = ref<DataView>('insar')
const selectedFiles = ref<NonNullable<UploadUserFile['raw']>[]>([])
const uploadRef = ref<UploadInstance>()
const graphNodes = ref<GraphNode[]>([])
const graphEdges = ref<GraphEdge[]>([])
const graphCanvas = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let graphPollTimer: number | null = null
const graphTask = ref<GraphTask | null>(null)
const graphNodeTypes = ref<string[]>([])
const graphNodeType = ref('')
const graphLimit = ref(20)
const includeTextKg = ref(true)
const expandedGraphNodeIds = ref<Set<string>>(new Set())
const loadedGraphNodeIds = ref<Set<string>>(new Set())
const loadingGraphNodeIds = ref<Set<string>>(new Set())
const selectedGraphNode = ref<GraphNode | null>(null)
const question = ref('')
const answer = ref<Answer | null>(null)
const loading = ref(false)
const dataLoading = ref(false)

const selectedDataset = computed(() => datasets.value.find((item) => item.id === selectedDatasetId.value))
const isRecordView = computed(() => ['insar', 'water_level', 'rainfall'].includes(dataView.value))
const isGisView = computed(() => dataView.value === 'gis_vector')
const graphBuilding = computed(() => graphTask.value?.status === 'queued' || graphTask.value?.status === 'running')
const graphTaskLastLog = computed(() => graphTask.value?.logs?.at(-1) || '')
const graphTypeColors = [
  '#166a5b',
  '#d97706',
  '#2563eb',
  '#9333ea',
  '#dc2626',
  '#0891b2',
  '#65a30d',
  '#be123c',
  '#475569',
  '#7c3aed'
]

const dataViewLabel = computed(() => {
  const labels: Record<DataView, string> = {
    insar: 'InSAR数据',
    water_level: '库水位数据',
    rainfall: '降雨数据',
    gis_vector: 'GIS矢量数据',
    documents: '文本资料',
    chunks: '文本切片'
  }
  return labels[dataView.value]
})

const recordRows = computed(() =>
  records.value.map((record, index) => {
    const row: Record<string, unknown> = {
      ...record.raw_fields,
      ...record.normalized_fields,
      id: record.id,
      row_number: (recordPage.value - 1) * recordPageSize.value + index + 1,
      timestamp: formatValue(record.timestamp),
      raw_fields: record.raw_fields
    }
    const longitude = record.normalized_fields.longitude ?? record.raw_fields.lon ?? record.raw_fields.longitude
    const latitude = record.normalized_fields.latitude ?? record.raw_fields.lat ?? record.raw_fields.latitude
    delete row.lon
    delete row.lat
    row.longitude = longitude
    row.latitude = latitude
    return row
  })
)

const recordColumns = computed(() => {
  const preferred = [
    'row_number',
    'timestamp',
    'landslide_name',
    'point_id',
    'station_name',
    'longitude',
    'latitude',
    'elevation',
    'displacement',
    'velocity',
    'water_level'
  ]
  const keys = new Set<string>()
  recordRows.value.forEach((row) => {
    Object.keys(row).forEach((key) => {
      if (!['id', 'raw_fields', 'data_type'].includes(key)) keys.add(key)
    })
  })
  return preferred.filter((key) => keys.has(key)).concat([...keys].filter((key) => !preferred.includes(key)))
})

const gisRows = computed(() =>
  gisFeatures.value.map((feature, index) => ({
    ...feature,
    display_index: (gisPage.value - 1) * gisPageSize.value + index + 1
  }))
)

const selectedChunk = computed(() => chunks.value.find((chunk) => chunk.id === selectedChunkId.value) || chunks.value[0])

async function refreshAll() {
  loading.value = true
  try {
    datasets.value = await api.listDatasets()
    if (!selectedDatasetId.value && datasets.value.length > 0) {
      selectedDatasetId.value = datasets.value[0].id
    }
    await refreshDatasetScope()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '刷新失败')
  } finally {
    loading.value = false
  }
}

async function refreshDatasetScope() {
  recordPage.value = 1
  gisPage.value = 1
  if (!selectedDatasetId.value) {
    imports.value = []
    records.value = []
    gisFeatures.value = []
    recordTotal.value = 0
    gisTotal.value = 0
    documents.value = []
    chunks.value = []
    graphNodes.value = []
    graphEdges.value = []
    return
  }
  await Promise.all([refreshImports(), refreshDataContent(), refreshGraph()])
}

async function createDataset() {
  if (!datasetName.value.trim()) {
    ElMessage.warning('请输入数据集名称')
    return
  }
  const dataset = await api.createDataset({ name: datasetName.value, description: datasetDescription.value })
  datasets.value.unshift(dataset)
  selectedDatasetId.value = dataset.id
  datasetName.value = ''
  datasetDescription.value = ''
  ElMessage.success('数据集已创建')
  await refreshDatasetScope()
}

async function deleteCurrentDataset() {
  if (!selectedDataset.value) {
    ElMessage.warning('请先选择数据集')
    return
  }
  await ElMessageBox.confirm(
    `确定删除数据集“${selectedDataset.value.name}”吗？该操作会删除其全部数据、文件记录和图谱节点。`,
    '删除数据集',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  )
  await api.deleteDataset(selectedDataset.value.id)
  ElMessage.success('数据集已删除')
  selectedDatasetId.value = ''
  await refreshAll()
}

function onFileChange(_file: UploadUserFile, fileList: UploadUserFile[]) {
  selectedFiles.value = fileList
    .map((item) => item.raw)
    .filter((item): item is NonNullable<UploadUserFile['raw']> => Boolean(item))
  if (selectedFiles.value.some((file) => isGisFile(file.name))) {
    uploadDataType.value = 'gis_vector'
  }
}

async function uploadFile() {
  if (!selectedDatasetId.value || selectedFiles.value.length === 0) {
    ElMessage.warning('请选择数据集和文件')
    return
  }
  const files = [...selectedFiles.value]
  const hasGisFiles = files.some((file) => isGisFile(file.name))
  const hasNonGisFiles = files.some((file) => !isGisFile(file.name))
  if (hasGisFiles && hasNonGisFiles) {
    ElMessage.warning('批量导入时请不要混合 GIS 文件和其他类型文件')
    return
  }
  if (hasGisFiles) {
    uploadDataType.value = 'gis_vector'
  }

  let completed = 0
  let queued = 0
  let failed = 0
  for (const file of files) {
    try {
      const result = await api.uploadFile(
        selectedDatasetId.value,
        uploadDataType.value,
        file,
        uploadDataType.value === 'gis_vector' ? uploadGisCategory.value : undefined
      )
      if (result.status === 'completed') {
        completed += 1
      } else {
        queued += 1
      }
    } catch (error) {
      failed += 1
      ElMessage.error(`${file.name} 导入失败：${error instanceof Error ? error.message : '未知错误'}`)
    }
  }

  selectedFiles.value = []
  uploadRef.value?.clearFiles()
  if (failed > 0) {
    ElMessage.warning(`批量导入完成：${completed} 个已入库，${queued} 个已创建任务，${failed} 个失败`)
  } else {
    ElMessage.success(`批量导入完成：${completed} 个已入库，${queued} 个已创建任务`)
  }
  await refreshImports()
  recordPage.value = 1
  gisPage.value = 1
  await refreshDataContent()
}

function isGisFile(filename: string) {
  const suffix = filename.toLowerCase().split('.').pop()
  return suffix === 'geojson' || suffix === 'json'
}

async function retryImport(taskId: string) {
  const result = await api.retryImport(taskId)
  ElMessage.success(result.status === 'completed' ? '数据已重新入库' : '已重新加入导入队列')
  await refreshImports()
  recordPage.value = 1
  gisPage.value = 1
  await refreshDataContent()
}

async function deleteImportTask(row: ImportTask) {
  await ElMessageBox.confirm('确定删除该导入任务及其产生的数据吗？', '删除导入数据', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消'
  })
  await api.deleteImport(row.id)
  ElMessage.success('导入任务及相关数据已删除')
  await refreshImports()
  await refreshDataContent()
}

async function deleteCurrentData() {
  if (!selectedDatasetId.value) {
    ElMessage.warning('请先选择数据集')
    return
  }
  await ElMessageBox.confirm(`确定删除当前数据集下的“${dataViewLabel.value}”吗？`, '删除数据', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消'
  })
  await api.deleteData(selectedDatasetId.value, dataView.value)
  ElMessage.success('数据已删除')
  recordPage.value = 1
  gisPage.value = 1
  await refreshDataContent()
  await refreshImports()
}

async function refreshImports() {
  imports.value = selectedDatasetId.value ? await api.listImports(selectedDatasetId.value) : []
}

async function refreshDataContent() {
  if (!selectedDatasetId.value) return
  dataLoading.value = true
  try {
    if (isRecordView.value) {
      const page = await api.listRecords(
        selectedDatasetId.value,
        dataView.value,
        recordPage.value,
        recordPageSize.value
      )
      records.value = page.items
      recordTotal.value = page.total
      gisFeatures.value = []
      documents.value = []
      chunks.value = []
      return
    }

    if (isGisView.value) {
      const page = await api.listGisFeatures(selectedDatasetId.value, gisPage.value, gisPageSize.value)
      gisFeatures.value = page.items
      gisTotal.value = page.total
      records.value = []
      documents.value = []
      chunks.value = []
      return
    }

    records.value = []
    gisFeatures.value = []
    recordTotal.value = 0
    gisTotal.value = 0
    if (dataView.value === 'documents') {
      documents.value = await api.listDocuments(selectedDatasetId.value)
      chunks.value = []
    } else {
      documents.value = []
      chunks.value = await api.listDocumentChunks(selectedDatasetId.value)
      if (!chunks.value.some((chunk) => chunk.id === selectedChunkId.value)) {
        selectedChunkId.value = chunks.value[0]?.id || ''
      }
    }
  } finally {
    dataLoading.value = false
  }
}

async function changeDataView() {
  recordPage.value = 1
  gisPage.value = 1
  await refreshDataContent()
}

async function changeRecordPage(page: number) {
  recordPage.value = page
  await refreshDataContent()
}

async function changeGisPage(page: number) {
  gisPage.value = page
  await refreshDataContent()
}

async function buildGraph() {
  if (!selectedDatasetId.value) {
    ElMessage.warning('请先选择数据集')
    return
  }
  const task = await api.buildGraph(selectedDatasetId.value, includeTextKg.value)
  graphTask.value = {
    id: task.task_id,
    dataset_id: selectedDatasetId.value,
    status: task.status,
    progress: 0,
    logs: [task.message],
    summary: {},
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString()
  }
  ElMessage.success('图谱生成任务已创建')
  startGraphPolling(task.task_id)
}

function startGraphPolling(taskId: string) {
  clearGraphPolling()
  const poll = async () => {
    try {
      const task = await api.getGraphTask(taskId)
      graphTask.value = task
      if (task.status === 'completed') {
        ElMessage.success('图谱生成完成')
        await refreshGraph()
        clearGraphPolling()
        return
      }
      if (task.status === 'failed') {
        ElMessage.error(task.error || graphTaskLastLog.value || '图谱生成失败')
        clearGraphPolling()
        return
      }
      graphPollTimer = window.setTimeout(poll, 1500)
    } catch (error) {
      ElMessage.error(error instanceof Error ? error.message : '图谱任务状态获取失败')
      clearGraphPolling()
    }
  }
  graphPollTimer = window.setTimeout(poll, 600)
}

function clearGraphPolling() {
  if (graphPollTimer !== null) {
    window.clearTimeout(graphPollTimer)
    graphPollTimer = null
  }
}

async function refreshGraph() {
  if (!selectedDatasetId.value) {
    graphNodes.value = []
    graphEdges.value = []
    graphNodeTypes.value = []
    expandedGraphNodeIds.value = new Set()
    loadedGraphNodeIds.value = new Set()
    return
  }
  const graph = await api.getGraph(selectedDatasetId.value, graphLimit.value, graphNodeType.value || undefined)
  graphNodes.value = graph.nodes
  graphEdges.value = graph.edges
  const rootIds = graph.nodes.filter((node) => isDatasetNode(node)).map((node) => node.id)
  expandedGraphNodeIds.value = new Set(rootIds)
  loadedGraphNodeIds.value = new Set(rootIds)
  selectedGraphNode.value = null
  await refreshGraphNodeTypes()
  await nextTick()
  renderGraph()
}

async function refreshGraphNodeTypes() {
  if (!selectedDatasetId.value) return
  const result = await api.getGraphNodeTypes(selectedDatasetId.value)
  graphNodeTypes.value = result.types
  if (graphNodeType.value && !graphNodeTypes.value.includes(graphNodeType.value)) {
    graphNodeType.value = ''
  }
}

async function ask() {
  if (!question.value.trim()) {
    ElMessage.warning('请输入问题')
    return
  }
  answer.value = await api.ask(question.value, selectedDatasetId.value || undefined)
}

function formatValue(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function columnLabel(key: string) {
  const labels: Record<string, string> = {
    row_number: '行号',
    timestamp: '时间',
    landslide_name: '滑坡体',
    point_id: '监测点',
    station_name: '站点',
    longitude: '经度',
    latitude: '纬度',
    elevation: '高程',
    displacement: '位移',
    velocity: '速率',
    water_level: '水位'
  }
  return labels[key] || key
}

function selectChunk(row: DocumentChunk) {
  selectedChunkId.value = row.id
}

function mergeGraph(nextGraph: { nodes: GraphNode[]; edges: GraphEdge[] }) {
  const nodeMap = new Map(graphNodes.value.map((node) => [node.id, node]))
  nextGraph.nodes.forEach((node) => nodeMap.set(node.id, node))
  graphNodes.value = [...nodeMap.values()]

  const edgeMap = new Map(graphEdges.value.map((edge) => [edge.id, edge]))
  nextGraph.edges.forEach((edge) => edgeMap.set(edge.id, edge))
  graphEdges.value = [...edgeMap.values()]
}

function isDatasetNode(node: GraphNode) {
  return node.id.startsWith('dataset:') || node.properties?.source_kind === 'dataset'
}

async function expandGraphNode(nodeId: string) {
  if (!selectedDatasetId.value || loadingGraphNodeIds.value.has(nodeId)) return
  if (!loadedGraphNodeIds.value.has(nodeId)) {
    const loadingNext = new Set(loadingGraphNodeIds.value)
    loadingNext.add(nodeId)
    loadingGraphNodeIds.value = loadingNext
    try {
      const graph = await api.getGraph(selectedDatasetId.value, graphLimit.value, undefined, nodeId)
      mergeGraph(graph)
      const loadedNext = new Set(loadedGraphNodeIds.value)
      loadedNext.add(nodeId)
      loadedGraphNodeIds.value = loadedNext
    } catch (error) {
      ElMessage.error(error instanceof Error ? error.message : '节点展开失败')
    } finally {
      const loadingDone = new Set(loadingGraphNodeIds.value)
      loadingDone.delete(nodeId)
      loadingGraphNodeIds.value = loadingDone
    }
  }
  const expandedNext = new Set(expandedGraphNodeIds.value)
  expandedNext.add(nodeId)
  expandedGraphNodeIds.value = expandedNext
  await nextTick()
  renderGraph()
}

function collapseGraphNode(nodeId: string) {
  const expandedNext = new Set(expandedGraphNodeIds.value)
  expandedNext.delete(nodeId)
  expandedGraphNodeIds.value = expandedNext
  nextTick(renderGraph)
}

function buildVisibleGraph() {
  const nodeMap = new Map(graphNodes.value.map((node) => [node.id, node]))
  const rootIds = graphNodes.value.filter((node) => isDatasetNode(node)).map((node) => node.id)
  const rootId = rootIds[0]
  const children = new Map<string, string[]>()
  const treeEdges = graphEdges.value.filter((edge) => nodeMap.has(edge.source) && nodeMap.has(edge.target))
  treeEdges.forEach((edge) => {
    const list = children.get(edge.source) || []
    list.push(edge.target)
    children.set(edge.source, list)
  })

  const visibleIds = new Set<string>()
  const visit = (nodeId: string) => {
    if (!nodeMap.has(nodeId) || visibleIds.has(nodeId)) return
    visibleIds.add(nodeId)
    if (!expandedGraphNodeIds.value.has(nodeId)) return
    ;(children.get(nodeId) || [])
      .filter((childId) => childId !== nodeId)
      .slice(0, graphLimit.value)
      .forEach((childId) => visit(childId))
  }

  if (rootId) visit(rootId)
  if (!rootId) graphNodes.value.forEach((node) => visit(node.id))

  const visibleNodes = graphNodes.value.filter((node) => visibleIds.has(node.id))

  return {
    nodes: visibleNodes,
    edges: treeEdges.filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target)),
    children
  }
}

function renderGraph() {
  if (!graphCanvas.value) return
  chart ||= echarts.init(graphCanvas.value)
  chart.off('click')
  chart.on('click', (params) => {
    const data = params.data as { id?: string } | undefined
    if (params.dataType === 'node' && data?.id) {
      const clickedNode = graphNodes.value.find((node) => node.id === String(data.id)) || null
      selectedGraphNode.value = clickedNode
      if (expandedGraphNodeIds.value.has(data.id) && clickedNode && !isDatasetNode(clickedNode)) {
        collapseGraphNode(data.id)
      } else {
        void expandGraphNode(data.id)
      }
    }
  })
  const visibleGraph = buildVisibleGraph()
  const visibleNodes = visibleGraph.nodes
  const visibleEdges = visibleGraph.edges
  const categories = Array.from(new Set(visibleNodes.map((node) => node.type))).map((name, index) => ({
    name,
    itemStyle: { color: graphTypeColors[index % graphTypeColors.length] }
  }))
  const categoryIndex = new Map(categories.map((category, index) => [category.name, index]))
  chart.clear()
  chart.setOption({
    tooltip: {
      formatter: (params: { dataType?: string; data?: { name?: string; category?: string }; value?: string }) => {
        if (params.dataType === 'edge') return params.value || ''
        return `${params.data?.name || ''}<br/>${params.data?.category || ''}`
      }
    },
    legend: [{ data: categories.map((item) => item.name), bottom: 0, type: 'scroll' }],
    series: [
      {
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        categories,
        force: { repulsion: 680, edgeLength: [120, 260], gravity: 0.04, friction: 0.22 },
        label: { show: true, position: 'right', overflow: 'truncate', width: 150 },
        edgeLabel: { show: visibleNodes.length <= 80, formatter: '{c}', fontSize: 10 },
        data: visibleNodes.map((node) => ({
          id: node.id,
          name: node.label,
          category: categoryIndex.get(node.type) ?? 0,
          value: node.type,
          symbolSize: isDatasetNode(node) ? 62 : node.type.includes('集合') ? 46 : 36
        })),
        links: visibleEdges.map((edge) => ({
          source: edge.source,
          target: edge.target,
          value: edge.label
        })),
        lineStyle: { color: '#94a3b8', opacity: 0.58, curveness: 0.18 },
        emphasis: { focus: 'adjacency' }
      }
    ]
  })
  chart.resize()
}

onMounted(refreshAll)
onBeforeUnmount(() => {
  clearGraphPolling()
  chart?.dispose()
})
watch([graphNodes, graphEdges], () => nextTick(renderGraph), { deep: true })
</script>

<template>
  <el-container class="layout">
    <el-aside width="280px" class="sidebar">
      <div class="brand">
        <div class="brand-mark">SM</div>
        <div>
          <h1>SlideMind</h1>
          <p>滑坡智能问答</p>
        </div>
      </div>

      <div class="nav-switch">
        <el-button :type="activePage === 'data' ? 'primary' : 'default'" @click="activePage = 'data'">
          数据管理
        </el-button>
        <el-button :type="activePage === 'analysis' ? 'primary' : 'default'" @click="activePage = 'analysis'">
          智能分析
        </el-button>
      </div>

      <el-select v-model="selectedDatasetId" placeholder="选择数据集" class="full" @change="refreshDatasetScope">
        <el-option v-for="dataset in datasets" :key="dataset.id" :label="dataset.name" :value="dataset.id" />
      </el-select>
      <el-button class="full danger-outline" :icon="Delete" :disabled="!selectedDatasetId" @click="deleteCurrentDataset">
        删除数据集
      </el-button>

      <el-divider />

      <el-form label-position="top">
        <el-form-item label="新建数据集">
          <el-input v-model="datasetName" placeholder="例如：三峡库区试验数据" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="datasetDescription" type="textarea" :rows="2" placeholder="描述" />
        </el-form-item>
        <el-button type="primary" :icon="FolderAdd" class="full" @click="createDataset">创建</el-button>
      </el-form>
    </el-aside>

    <el-main v-loading="loading" class="main">
      <div class="topbar">
        <div>
          <h2>{{ selectedDataset?.name || '未选择数据集' }}</h2>
          <p>{{ activePage === 'data' ? '上传、查看和删除数据' : '生成知识图谱并进行智能问答' }}</p>
        </div>
        <el-button :icon="Refresh" circle @click="refreshAll" />
      </div>

      <template v-if="activePage === 'data'">
        <el-row :gutter="16">
          <el-col :span="10">
            <section class="panel">
              <div class="panel-title">
                <el-icon><UploadFilled /></el-icon>
                <span>数据输入</span>
              </div>
              <el-segmented
                v-model="uploadDataType"
                :options="[
                  { label: 'InSAR', value: 'insar' },
                  { label: '库水位', value: 'water_level' },
                  { label: '降雨', value: 'rainfall' },
                  { label: 'GIS矢量', value: 'gis_vector' },
                  { label: '文本', value: 'document' }
                ]"
              />
              <el-form v-if="uploadDataType === 'gis_vector'" label-position="top" class="inline-form">
                <el-form-item label="GIS图层类别">
                  <el-select v-model="uploadGisCategory">
                    <el-option label="行政区 area" value="area" />
                    <el-option label="建筑 build" value="build" />
                    <el-option label="交通 traffic" value="traffic" />
                    <el-option label="水域 water" value="water" />
                    <el-option label="其他 other" value="other" />
                  </el-select>
                </el-form-item>
              </el-form>
              <el-upload ref="uploadRef" class="upload" drag multiple :auto-upload="false" :on-change="onFileChange">
                <el-icon class="upload-icon"><UploadFilled /></el-icon>
                <div>拖入或点击选择 csv、xlsx、geojson、json、txt、docx、pdf</div>
              </el-upload>
              <el-button
                type="primary"
                class="full"
                :disabled="!selectedDatasetId || selectedFiles.length === 0"
                @click="uploadFile"
              >
                {{ selectedFiles.length > 1 ? `批量导入 ${selectedFiles.length} 个文件` : '提交导入' }}
              </el-button>
            </section>
          </el-col>

          <el-col :span="14">
            <section class="panel">
              <div class="panel-title">
                <el-icon><DataAnalysis /></el-icon>
                <span>导入任务</span>
                <el-button size="small" :icon="Refresh" @click="refreshImports">刷新</el-button>
              </div>
              <el-table :data="imports" height="280" size="small" empty-text="当前数据集暂无导入任务">
                <el-table-column prop="data_type" label="类型" width="110" />
                <el-table-column label="GIS类别" width="110">
                  <template #default="{ row }">{{ row.gis_category || '-' }}</template>
                </el-table-column>
                <el-table-column prop="status" label="状态" width="110">
                  <template #default="{ row }">
                    <el-tag :type="row.status === 'completed' ? 'success' : row.status === 'failed' ? 'danger' : 'info'">
                      {{ row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="日志" min-width="220">
                  <template #default="{ row }">{{ row.logs?.at(-1) || '-' }}</template>
                </el-table-column>
                <el-table-column prop="updated_at" label="更新时间" width="180" />
                <el-table-column label="操作" width="190" fixed="right">
                  <template #default="{ row }">
                    <el-button v-if="row.status !== 'running'" size="small" @click="retryImport(row.id)">重新处理</el-button>
                    <el-button size="small" type="danger" :icon="Delete" @click="deleteImportTask(row)" />
                  </template>
                </el-table-column>
              </el-table>
            </section>
          </el-col>
        </el-row>

        <section class="panel data-panel">
          <div class="panel-title">
            <el-icon><Tickets /></el-icon>
            <span>数据内容：{{ selectedDataset?.name || '未选择数据集' }}</span>
            <el-button size="small" :icon="Refresh" :disabled="!selectedDatasetId" @click="refreshDataContent">刷新</el-button>
            <el-button size="small" type="danger" :icon="Delete" :disabled="!selectedDatasetId" @click="deleteCurrentData">
              删除当前数据
            </el-button>
          </div>

          <el-segmented
            v-model="dataView"
            class="data-tabs"
            :options="[
              { label: 'InSAR数据', value: 'insar' },
              { label: '库水位数据', value: 'water_level' },
              { label: '降雨数据', value: 'rainfall' },
              { label: 'GIS矢量', value: 'gis_vector' },
              { label: '文本资料', value: 'documents' },
              { label: '文本切片', value: 'chunks' }
            ]"
            @change="changeDataView"
          />

          <el-empty v-if="!selectedDatasetId" description="请先选择数据集" />

          <template v-else-if="isRecordView">
            <el-table
              v-loading="dataLoading"
              :data="recordRows"
              height="360"
              size="small"
              border
              empty-text="当前数据集暂无此类表格数据"
            >
              <el-table-column
                v-for="column in recordColumns"
                :key="column"
                :prop="column"
                :label="columnLabel(column)"
                min-width="120"
                show-overflow-tooltip
              >
                <template #default="{ row }">{{ formatValue(row[column]) }}</template>
              </el-table-column>
              <el-table-column label="原始字段" width="110" fixed="right">
                <template #default="{ row }">
                  <el-popover placement="left" width="520" trigger="click">
                    <pre class="json-preview">{{ JSON.stringify(row.raw_fields, null, 2) }}</pre>
                    <template #reference>
                      <el-button size="small">查看</el-button>
                    </template>
                  </el-popover>
                </template>
              </el-table-column>
            </el-table>
            <div class="pagination-bar">
              <span>共 {{ recordTotal }} 条，每页 {{ recordPageSize }} 条</span>
              <el-pagination
                background
                layout="prev, pager, next"
                :current-page="recordPage"
                :page-size="recordPageSize"
                :total="recordTotal"
                @current-change="changeRecordPage"
              />
            </div>
          </template>

          <template v-else-if="isGisView">
            <el-table
              v-loading="dataLoading"
              :data="gisRows"
              height="360"
              size="small"
              border
              empty-text="当前数据集暂无GIS矢量数据"
            >
              <el-table-column prop="display_index" label="序号" width="80" />
              <el-table-column prop="gis_category_name" label="类别" width="110" />
              <el-table-column prop="layer_name" label="图层" min-width="160" show-overflow-tooltip />
              <el-table-column prop="geometry_type" label="几何类型" width="120" />
              <el-table-column label="中心点" min-width="170">
                <template #default="{ row }">
                  {{ row.centroid ? `${row.centroid.longitude.toFixed(6)}, ${row.centroid.latitude.toFixed(6)}` : '-' }}
                </template>
              </el-table-column>
              <el-table-column label="属性" min-width="220" show-overflow-tooltip>
                <template #default="{ row }">{{ JSON.stringify(row.properties) }}</template>
              </el-table-column>
              <el-table-column label="空间对象" width="120" fixed="right">
                <template #default="{ row }">
                  <el-popover placement="left" width="560" trigger="click">
                    <pre class="json-preview">{{ JSON.stringify({ properties: row.properties, geometry: row.geometry, bbox: row.bbox }, null, 2) }}</pre>
                    <template #reference>
                      <el-button size="small" :icon="Location">查看</el-button>
                    </template>
                  </el-popover>
                </template>
              </el-table-column>
            </el-table>
            <div class="pagination-bar">
              <span>共 {{ gisTotal }} 个要素，每页 {{ gisPageSize }} 个</span>
              <el-pagination
                background
                layout="prev, pager, next"
                :current-page="gisPage"
                :page-size="gisPageSize"
                :total="gisTotal"
                @current-change="changeGisPage"
              />
            </div>
          </template>

          <el-table
            v-else-if="dataView === 'documents'"
            v-loading="dataLoading"
            :data="documents"
            height="360"
            size="small"
            border
            empty-text="当前数据集暂无文本资料"
          >
            <el-table-column prop="title" label="文件名" min-width="220" show-overflow-tooltip />
            <el-table-column prop="source_file_id" label="文件ID" min-width="220" show-overflow-tooltip />
            <el-table-column prop="created_at" label="入库时间" width="180" />
          </el-table>

          <el-row v-else :gutter="12" v-loading="dataLoading">
            <el-col :span="10">
              <el-table
                :data="chunks"
                height="360"
                size="small"
                border
                highlight-current-row
                empty-text="当前数据集暂无文本切片"
                @row-click="selectChunk"
              >
                <el-table-column prop="chunk_index" label="序号" width="80" />
                <el-table-column prop="text" label="内容预览" min-width="220" show-overflow-tooltip />
              </el-table>
            </el-col>
            <el-col :span="14">
              <div class="chunk-preview">
                <div class="chunk-title">
                  <el-icon><Document /></el-icon>
                  <span>切片预览</span>
                </div>
                <p>{{ selectedChunk?.text || '暂无可预览内容' }}</p>
              </div>
            </el-col>
          </el-row>
        </section>
      </template>

      <template v-else>
        <section class="panel graph-panel">
          <div class="panel-title">
            <el-icon><Connection /></el-icon>
            <span>知识图谱：{{ selectedDataset?.name || '未选择数据集' }}</span>
            <el-button size="small" type="primary" :loading="graphBuilding" :disabled="!selectedDatasetId" @click="buildGraph">
              生成
            </el-button>
          </div>
          <div v-if="graphTask" class="graph-task">
            <div class="graph-task-row">
              <el-tag size="small" :type="graphTask.status === 'completed' ? 'success' : graphTask.status === 'failed' ? 'danger' : 'info'">
                {{ graphTask.status }}
              </el-tag>
              <span>{{ graphTaskLastLog || '等待任务日志' }}</span>
            </div>
            <el-progress :percentage="graphTask.progress || 0" :status="graphTask.status === 'failed' ? 'exception' : graphTask.status === 'completed' ? 'success' : undefined" />
            <div v-if="graphTask.summary?.text_kg_enabled !== undefined" class="graph-task-summary">
              文本融合：{{ graphTask.summary.text_kg_enabled ? '开启' : '关闭' }}，
              chunk {{ graphTask.summary.text_chunks_processed || 0 }}/{{ graphTask.summary.text_chunks_total || 0 }}，
              五元组 {{ graphTask.summary.text_tuple_count || 0 }}，
              区域匹配 {{ graphTask.summary.text_region_matched || 0 }}，
              未定位 {{ graphTask.summary.text_region_unmatched || 0 }}
            </div>
          </div>
          <div class="graph-controls">
            <el-select v-model="graphNodeType" placeholder="全部节点类型" clearable @change="refreshGraph">
              <el-option label="全部节点类型" value="" />
              <el-option v-for="type in graphNodeTypes" :key="type" :label="type" :value="type" />
            </el-select>
            <el-input-number v-model="graphLimit" aria-label="每次展开节点数" :min="1" :max="100" :step="5" @change="refreshGraph" />
            <el-switch v-model="includeTextKg" active-text="融合文本知识" inactive-text="仅空间图谱" />
            <el-button size="small" :icon="Refresh" :disabled="!selectedDatasetId" @click="refreshGraph">刷新图谱</el-button>
          </div>
          <div class="graph-workspace">
            <div class="graph-box">
              <div v-if="graphNodes.length === 0" class="empty">当前数据集暂无图谱节点</div>
              <div ref="graphCanvas" class="graph-canvas" />
            </div>
            <aside class="node-detail">
              <div class="node-detail-title">
                <el-icon><Tickets /></el-icon>
                <span>节点详情</span>
              </div>
              <template v-if="selectedGraphNode">
                <h3>{{ selectedGraphNode.label }}</h3>
                <el-tag>{{ selectedGraphNode.type }}</el-tag>
                <pre class="json-preview">{{ JSON.stringify(selectedGraphNode.properties, null, 2) }}</pre>
              </template>
              <el-empty v-else description="点击图谱节点查看内容" />
            </aside>
          </div>
          <div class="edge-summary">{{ graphNodes.length }} 个已加载节点，{{ graphEdges.length }} 条关系，每次展开最多 {{ graphLimit }} 个关联节点</div>
        </section>

        <section class="panel qa-panel">
          <div class="panel-title">
            <el-icon><ChatDotRound /></el-icon>
            <span>智能问答</span>
          </div>
          <el-input
            v-model="question"
            type="textarea"
            :rows="4"
            placeholder="例如：库水位变化是否和位移异常有关？"
          />
          <el-button type="primary" class="full" @click="ask">提问</el-button>
          <div v-if="answer" class="answer">
            <el-tag>{{ answer.route }}</el-tag>
            <p>{{ answer.answer }}</p>
            <pre>{{ JSON.stringify(answer.sources.slice(0, 3), null, 2) }}</pre>
          </div>
        </section>
      </template>
    </el-main>
  </el-container>
</template>
