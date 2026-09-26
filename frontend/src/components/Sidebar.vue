<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted } from "vue";
import type {
  Task,
  SummaryMode,
  LLMProvider,
  LLMSettings,
  TranscriptionSettings,
  SummarizationSettings,
} from "../types";
import { wb, messageOf } from "../utils/workbench";
import ThemeSelector from "./ThemeSelector.vue";
const videoUrl = defineModel<string>("videoUrl", { required: true });
const selectedFile = defineModel<File | null>("selectedFile", {
  default: null,
});
const localFilePath = defineModel<string>("localFilePath", { default: "" });
defineModel<string>("quality", { default: "audio_only" });
const summaryMode = defineModel<SummaryMode>("summaryMode", {
  default: "auto",
});
const isSidebarOpen = defineModel<boolean>("isSidebarOpen", { required: true });
const props = defineProps<{
  isLocalClient: boolean;
  tasks: Task[];
  selectedTask: Task | null;
  isSubmitting: boolean;
  llmProviders: LLMProvider[];
  llmSettings: LLMSettings | null;
  isUpdatingLlmSettings: boolean;
  isTestingLlm: boolean;
  transcriptionSettings: TranscriptionSettings | null;
  isUpdatingTranscriptionSettings: boolean;
  summarizationSettings: SummarizationSettings | null;
  isUpdatingSummarizationSettings: boolean;
}>();

const emit = defineEmits<{
  submit: [];
  cancelSubmit: [];
  selectTask: [task: Task];
  deleteTask: [taskId: string];
  showInfo: [task: Task];
  openSettings: [];
  focusSearchMatch: [
    payload: {
      taskId: string;
      keyword: string;
      source: "topic" | "summary";
      requestId: number;
    },
  ];
  updateLlmSettings: [
    payload: {
      provider: string;
      base_url?: string;
      api_key?: string;
      model_id?: string;
      temperature?: number;
    },
  ];
  updateTranscriptionSettings: [
    payload: {
      device?: "cpu" | "cuda";
      model_source?: "auto_download" | "manual_path";
      model_size?: "tiny" | "base" | "small" | "medium" | "large";
      model_path?: string;
      enable_bilibili_subtitle_fetch?: boolean;
      bilibili_sessdata?: string;
      clear_bilibili_sessdata?: boolean;
    },
  ];
  updateSummarizationSettings: [
    payload: {
      chunk_target_duration_sec?: number;
      chunk_min_duration_sec?: number;
      chunk_max_duration_sec?: number;
      boundary_jump_sec?: number;
      auto_chunk_min_audio_duration_sec?: number;
      auto_chunk_min_transcript_lines?: number;
      max_agent_value_chars?: number;
      fallback_to_standard_on_agent_error?: boolean;
    },
  ];
  startTestLlm: [];
}>();

const tab = ref<"tasks" | "theme">("tasks"),
  search = ref(""),
  filter = ref(""),
  collection = ref("");
const rows = ref<Task[]>([]),
  total = ref(0),
  offset = ref(0),
  loading = ref(false),
  error = ref("");
const showNew = ref(true);
const duplicates = ref<any[]>([]);
let previewTimer: ReturnType<typeof setTimeout> | undefined;
let previewSeq = 0;
watch(videoUrl, () => {
  const id = ++previewSeq;
  duplicates.value = [];
  if (previewTimer) clearTimeout(previewTimer);
  const match = videoUrl.value.match(/https?:\/\/[^\s]+/);
  if (!match) return;
  previewTimer = setTimeout(async () => {
    try {
      const data = (
        await wb.get("/source-preview", { params: { url: match[0] } })
      ).data;
      if (id === previewSeq) duplicates.value = data.duplicates;
    } catch {}
  }, 450);
});
const labels: Record<string, string> = {
  PENDING: "排队",
  DOWNLOADING: "获取内容",
  UPLOADING: "准备文件",
  TRANSCRIBING: "语音识别",
  SUMMARIZING: "生成笔记",
  COMPLETED: "完成",
  FAILED: "待处理",
};
let seq = 0,
  timer: ReturnType<typeof setTimeout> | undefined;
async function load() {
  const request = ++seq;
  loading.value = true;
  try {
    const r = (
      await wb.get("/tasks", {
        params: {
          q: search.value,
          status: filter.value,
          collection: collection.value,
          offset: offset.value,
          limit: 40,
        },
      })
    ).data;
    if (request === seq) {
      rows.value = r.items;
      total.value = r.total;
      error.value = "";
    }
  } catch (e) {
    if (request === seq) error.value = messageOf(e);
  } finally {
    if (request === seq) loading.value = false;
  }
}
watch([search, filter, collection], () => {
  offset.value = 0;
  if (timer) clearTimeout(timer);
  timer = setTimeout(load, 300);
});
watch(
  () => props.tasks,
  () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(load, 900);
  },
  { deep: true },
);
onMounted(load);
onUnmounted(() => {
  seq++;
  if (timer) clearTimeout(timer);
  if (previewTimer) clearTimeout(previewTimer);
  previewSeq++;
});
const select = (task: Task) => {
  emit("selectTask", task);
  if (search.value)
    emit("focusSearchMatch", {
      taskId: task.id,
      keyword: search.value,
      source: "summary",
      requestId: Date.now(),
    });
};
const choose = (e: Event) => {
  selectedFile.value = (e.target as HTMLInputElement).files?.[0] || null;
  localFilePath.value = "";
};
async function subtitles(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0];
  if (!f) return;
  try {
    const task = (
      await wb.post("/tasks/import-subtitles", {
        text: await f.text(),
        format: f.name.endsWith(".vtt") ? "vtt" : "srt",
      })
    ).data;
    emit("selectTask", task);
    await load();
  } catch (e) {
    error.value = messageOf(e);
  }
}
</script>
<template>
  <aside
    class="fixed md:static inset-y-0 left-0 z-40 w-[90%] max-w-[350px] md:w-[350px] h-full bg-white border-r flex flex-col shrink-0 transition-transform"
    :class="
      isSidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'
    "
  >
    <header class="px-5 py-4 border-b flex items-center justify-between">
      <div>
        <h1 class="text-lg font-bold text-slate-900">声文智汇</h1>
        <p class="text-xs text-slate-500">从音视频到可核对的学习笔记</p>
      </div>
      <div class="flex gap-2">
        <button class="btn" @click="emit('openSettings')">设置</button
        ><button
          class="md:hidden btn"
          @click="isSidebarOpen = false"
          aria-label="关闭侧栏"
        >
          ✕
        </button>
      </div>
    </header>
    <nav class="flex gap-2 px-4 pt-3">
      <button
        class="btn"
        :class="tab === 'tasks' ? 'bg-blue-50 text-blue-700' : ''"
        @click="tab = 'tasks'"
      >
        任务与课程</button
      ><button class="btn" @click="tab = 'theme'">阅读主题</button>
    </nav>
    <div v-if="tab === 'theme'" class="p-4 overflow-auto">
      <ThemeSelector />
    </div>
    <template v-else>
      <div class="p-4 border-b space-y-3">
        <button
          class="font-medium text-sm w-full text-left"
          @click="showNew = !showNew"
        >
          {{ showNew ? "−" : "＋" }} 新建任务
        </button>
        <div v-if="showNew" class="space-y-3">
          <input
            v-model="videoUrl"
            class="input"
            placeholder="粘贴视频链接或分享文本"
            aria-label="视频链接"
            @keydown.enter="emit('submit')"
          />
          <div
            v-if="duplicates.length"
            class="rounded-lg bg-amber-50 p-2 text-xs text-amber-800"
          >
            发现相同来源的历史任务。继续处理将创建新任务。<button
              class="block mt-1 underline"
              @click="emit('selectTask', duplicates[0])"
            >
              查看最近一次结果
            </button>
          </div>
          <input
            v-if="isLocalClient"
            v-model="localFilePath"
            class="input"
            placeholder="或粘贴本地文件 / 文件夹路径"
            aria-label="本地路径"
            @keydown.enter="emit('submit')"
          />
          <div class="flex gap-3 text-xs">
            <label class="text-blue-600 cursor-pointer"
              >选择音视频<input
                type="file"
                accept="audio/*,video/*"
                class="hidden"
                @change="choose" /></label
            ><label class="text-blue-600 cursor-pointer"
              >导入 SRT / VTT<input
                type="file"
                accept=".srt,.vtt"
                class="hidden"
                @change="subtitles"
            /></label>
          </div>
          <div
            v-if="selectedFile"
            class="text-xs flex justify-between bg-slate-50 p-2 rounded"
          >
            <span class="truncate">{{ selectedFile.name }}</span
            ><button @click="selectedFile = null" aria-label="移除文件">
              ✕
            </button>
          </div>
          <select v-model="summaryMode" class="input" aria-label="处理方式">
            <option value="auto">自动选择处理方式</option>
            <option value="standard">快速整理</option>
            <option value="agent">深度笔记（分块处理）</option>
          </select>
          <p class="text-xs text-slate-500">
            {{
              transcriptionSettings?.enable_bilibili_subtitle_fetch
                ? "优先获取字幕，无字幕时使用本地模型"
                : "使用本地语音识别"
            }}。
          </p>
          <button
            class="w-full rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-sm py-3 disabled:opacity-50"
            :disabled="
              isSubmitting ||
              (!videoUrl.trim() && !localFilePath.trim() && !selectedFile)
            "
            @click="emit('submit')"
          >
            {{ isSubmitting ? "正在提交…" : "开始处理" }}</button
          ><button
            v-if="isSubmitting"
            class="btn w-full"
            @click="emit('cancelSubmit')"
          >
            取消提交
          </button>
        </div>
      </div>
      <div class="p-4 space-y-2">
        <input
          v-model="search"
          class="input"
          placeholder="搜索标题、原文和总结"
          aria-label="搜索全部任务"
        />
        <div class="flex gap-2">
          <select v-model="filter" class="input" aria-label="筛选状态">
            <option value="">全部状态</option>
            <option
              v-for="(label, status) in labels"
              :key="status"
              :value="status"
            >
              {{ label }}
            </option></select
          ><input
            v-model="collection"
            class="input"
            placeholder="课程集合"
            aria-label="筛选课程集合"
          />
        </div>
        <p class="text-xs text-slate-400">
          {{ total }} 个任务 · {{ loading ? "正在同步…" : "正文按需加载" }}
        </p>
        <p v-if="error" role="alert" class="text-xs text-red-600">
          {{ error }}
        </p>
      </div>
      <div class="flex-1 overflow-y-auto px-3 space-y-2 pb-3">
        <article
          v-for="task in rows"
          :key="task.id"
          class="rounded-xl border p-3 group"
          :class="
            selectedTask?.id === task.id
              ? 'border-blue-300 bg-blue-50'
              : 'border-transparent hover:bg-slate-50'
          "
        >
          <button class="text-left w-full" @click="select(task)">
            <div class="flex justify-between mb-2 text-[11px]">
              <span
                :class="
                  task.status === 'FAILED'
                    ? 'text-amber-700'
                    : task.status === 'COMPLETED'
                      ? 'text-emerald-700'
                      : 'text-blue-600'
                "
                >{{ labels[task.status] || task.status }}</span
              ><span class="text-slate-400">{{
                new Date(task.created_at).toLocaleDateString()
              }}</span>
            </div>
            <p class="text-sm font-medium text-slate-700 line-clamp-2">
              {{ task.topic || task.title || "等待解析来源" }}
            </p>
            <progress
              v-if="!['COMPLETED', 'FAILED'].includes(task.status)"
              class="w-full h-1 mt-2"
              :value="task.progress"
              max="100"
            ></progress>
          </button>
          <div class="flex justify-end gap-3 text-[11px] mt-2 text-slate-400">
            <button @click="emit('showInfo', task)">详情</button
            ><button @click="emit('deleteTask', task.id)">删除</button>
          </div>
        </article>
        <p
          v-if="!rows.length && !loading"
          class="text-sm text-slate-400 text-center py-8"
        >
          没有匹配的任务
        </p>
      </div>
      <footer class="border-t p-3 flex items-center justify-between text-xs">
        <button
          class="btn"
          :disabled="offset === 0 || loading"
          @click="
            offset = Math.max(0, offset - 40);
            load();
          "
        >
          上一页</button
        ><span
          >{{ Math.floor(offset / 40) + 1 }} /
          {{ Math.max(1, Math.ceil(total / 40)) }}</span
        ><button
          class="btn"
          :disabled="offset + 40 >= total || loading"
          @click="
            offset += 40;
            load();
          "
        >
          下一页
        </button>
      </footer>
    </template>
  </aside>
</template>
<style scoped>
.btn {
  @apply text-xs px-3 py-2 rounded-lg border border-slate-200 hover:bg-slate-50 disabled:opacity-40;
}
.input {
  @apply w-full border border-slate-200 bg-slate-50 rounded-lg px-3 py-2.5 text-sm;
}
</style>
