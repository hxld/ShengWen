<script setup lang="ts">
import { ref, computed, watch } from "vue";
import type { Task } from "../types";
import { wb, messageOf } from "../utils/workbench";
import { buildTimestampJumpUrl } from "../utils/videoTimeJump";
const props = defineProps<{ task: Task }>();
const emit = defineEmits<{ refresh: [] }>();
const expanded = ref(false),
  busy = ref(false),
  error = ref(""),
  notice = ref(""),
  editing = ref(false);
const details = ref<any>({ segments: [], meta: {}, versions: [], events: [] });
const question = ref(""),
  answer = ref<any>(null),
  template = ref("course"),
  collection = ref(""),
  tags = ref("");
const page = ref(0),
  selectedVersion = ref("");
const active = computed(() =>
  [
    "PENDING",
    "DOWNLOADING",
    "UPLOADING",
    "TRANSCRIBING",
    "SUMMARIZING",
  ].includes(props.task.status),
);
const segments = computed<any[]>(() =>
  details.value.segments.slice(page.value * 40, (page.value + 1) * 40),
);
const version = computed(() =>
  details.value.versions.find((v: any) => v.id === selectedVersion.value),
);
const timestamp = (t: number) =>
  new Date(Math.max(0, t) * 1000).toISOString().slice(11, 19);
const jump = (s: any) =>
  buildTimestampJumpUrl(
    s.source_url || props.task.video_url,
    s.source_start ?? s.start,
  );
async function load() {
  const id = props.task.id;
  const result = (await wb.get(`/tasks/${id}/details`)).data;
  if (id !== props.task.id) return;
  details.value = result;
  collection.value = result.meta.collection || "";
  tags.value = result.meta.tags || "";
}
async function run(fn: () => Promise<unknown>, success = "操作完成") {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    await fn();
    await load();
    emit("refresh");
    notice.value = success;
  } catch (e) {
    error.value = messageOf(e);
  } finally {
    busy.value = false;
  }
}
async function toggle() {
  expanded.value = !expanded.value;
  if (expanded.value)
    try {
      await load();
    } catch (e) {
      error.value = messageOf(e);
    }
}
async function upload(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0];
  if (!file) return;
  const content = await file.text();
  await run(
    () =>
      wb.put(`/tasks/${props.task.id}/transcript`, {
        text: content,
        format: file.name.toLowerCase().endsWith(".vtt") ? "vtt" : "srt",
      }),
    "字幕已导入；原总结保留，可重新生成",
  );
}
async function ask() {
  await run(async () => {
    answer.value = (
      await wb.post(
        `/tasks/${props.task.id}/ask`,
        { question: question.value },
        { timeout: 120000 },
      )
    ).data;
  }, "回答已生成，请核对引用");
}
watch(
  () => props.task.id,
  () => {
    expanded.value = false;
    details.value = { segments: [], meta: {}, versions: [], events: [] };
    answer.value = null;
    question.value = "";
    page.value = 0;
    editing.value = false;
    error.value = "";
    notice.value = "";
    selectedVersion.value = "";
  },
);
</script>
<template>
  <section
    class="mb-5 rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden"
  >
    <div class="flex flex-wrap items-center justify-between gap-2 p-3">
      <button class="text-sm font-semibold text-slate-700" @click="toggle">
        {{ expanded ? "收起" : "展开" }} · 核对与学习
      </button>
      <div class="flex gap-2">
        <button
          v-if="active"
          class="btn text-amber-700"
          :disabled="busy"
          @click="
            run(
              () => wb.post(`/tasks/${task.id}/cancel`),
              '已请求取消，保留原文与已有结果',
            )
          "
        >
          取消计算</button
        ><button
          v-if="task.status === 'FAILED'"
          class="btn text-blue-700"
          :disabled="busy"
          @click="
            run(
              () => wb.post(`/tasks/${task.id}/retry`),
              '已从可用阶段重新排队',
            )
          "
        >
          恢复 / 重试
        </button>
      </div>
    </div>
    <p
      v-if="error"
      role="alert"
      class="mx-3 mb-3 p-3 rounded-lg bg-red-50 text-red-700 text-sm"
    >
      {{ error }}
    </p>
    <p v-if="notice" role="status" class="mx-3 mb-3 text-sm text-emerald-700">
      {{ notice }}
    </p>
    <div v-if="expanded" class="border-t p-4 space-y-5">
      <p
        v-if="details.meta.summary_stale"
        class="text-amber-700 bg-amber-50 p-3 rounded-lg text-sm"
      >
        原文已修改，当前总结基于旧版本。可在下方重新生成。
      </p>
      <div v-if="details.meta.coverage" class="text-xs text-slate-600">
        内容覆盖：<span
          v-for="p in details.meta.coverage"
          :key="p.part"
          class="mr-2"
          >P{{ p.part }} ·
          {{ p.source === "subtitle" ? "字幕" : "语音识别" }}</span
        ><b>{{
          details.meta.complete ? "全部选中内容已获取" : "仍有内容待补齐"
        }}</b>
      </div>
      <div class="flex gap-2">
        <input
          v-model="collection"
          class="input"
          placeholder="课程 / 集合名称"
          aria-label="课程集合"
        /><input
          v-model="tags"
          class="input"
          placeholder="标签，以逗号分隔"
          aria-label="标签"
        /><button
          class="btn shrink-0"
          :disabled="busy"
          @click="
            run(
              () => wb.put(`/tasks/${task.id}/meta`, { collection, tags }),
              '分类已保存',
            )
          "
        >
          保存分类
        </button>
      </div>
      <div class="rounded-xl bg-slate-50 p-3 space-y-3">
        <h4 class="font-medium text-sm">重新生成笔记</h4>
        <div class="flex gap-2">
          <select v-model="template" class="input">
            <option value="course">课程笔记</option>
            <option value="meeting">会议纪要</option>
            <option value="tutorial">操作教程</option>
            <option value="review">复习卡片</option></select
          ><button
            class="btn shrink-0"
            :disabled="busy || active || !task.transcript"
            @click="
              run(
                () => wb.post(`/tasks/${task.id}/generate`, { template }),
                '已开始生成，旧结果已存入版本历史',
              )
            "
          >
            按模板生成
          </button>
        </div>
        <p class="text-xs text-slate-500">
          会调用当前 AI 接口；旧结果保留在版本历史。
        </p>
      </div>
      <details class="border rounded-xl p-3">
        <summary class="font-medium text-sm cursor-pointer">
          原文片段 · {{ details.segments.length }} 段
        </summary>
        <div class="mt-3 space-y-3">
          <div class="flex flex-wrap gap-2">
            <a
              class="btn"
              :href="`${wb.defaults.baseURL}/tasks/${task.id}/subtitles?format=srt`"
              download
              >SRT 导出</a
            ><a
              class="btn"
              :href="`${wb.defaults.baseURL}/tasks/${task.id}/subtitles?format=vtt`"
              download
              >VTT 导出</a
            ><label class="btn cursor-pointer" :class="{ 'opacity-40': active }"
              >导入字幕<input
                type="file"
                accept=".srt,.vtt"
                :disabled="busy || active"
                class="hidden"
                @change="upload" /></label
            ><button
              class="btn"
              :disabled="busy || active"
              @click="editing = !editing"
            >
              {{ editing ? "结束编辑" : "编辑文字" }}</button
            ><button
              v-if="editing"
              class="btn"
              :disabled="busy"
              @click="
                run(
                  () =>
                    wb.put(`/tasks/${task.id}/transcript`, {
                      text: JSON.stringify(details.segments),
                      format: 'segments',
                    }),
                  '修订已保存，旧原文已归档',
                )
              "
            >
              保存修订
            </button>
          </div>
          <p class="text-xs text-slate-500">
            历史纯文本的结束时间按相邻片段推算；带分 P
            的来源跳转使用原视频时间。
          </p>
          <div class="max-h-80 overflow-auto divide-y">
            <div v-for="(s, i) in segments" :key="page * 40 + i" class="py-3">
              <div class="text-xs text-blue-600 mb-1">
                <a
                  v-if="jump(s)"
                  :href="jump(s)!"
                  target="_blank"
                  rel="noopener noreferrer"
                  >{{ s.part ? `P${s.part} · ` : ""
                  }}{{ timestamp(s.source_start ?? s.start) }} ↗</a
                ><span v-else>{{ timestamp(s.start) }}</span
                ><span class="text-slate-400 ml-2"
                  >片段 {{ page * 40 + i + 1
                  }}{{ s.timing_inferred ? " · 时间推算" : "" }}</span
                >
              </div>
              <textarea
                v-if="editing"
                v-model="s.text"
                class="input"
                rows="2"
              ></textarea>
              <p v-else class="text-sm whitespace-pre-wrap">{{ s.text }}</p>
            </div>
          </div>
          <div class="flex gap-2 text-xs">
            <button class="btn" :disabled="page === 0" @click="page--">
              上一页</button
            ><span class="py-2"
              >{{ page + 1 }} /
              {{ Math.max(1, Math.ceil(details.segments.length / 40)) }}</span
            ><button
              class="btn"
              :disabled="(page + 1) * 40 >= details.segments.length"
              @click="page++"
            >
              下一页
            </button>
          </div>
        </div>
      </details>
      <div class="space-y-3">
        <h4 class="font-medium text-sm">问这段内容</h4>
        <div class="flex gap-2">
          <input
            v-model="question"
            class="input"
            placeholder="例如：作者为什么推荐这个方案？"
            @keydown.enter="ask"
          /><button
            class="btn shrink-0"
            :disabled="busy || question.trim().length < 2 || !task.transcript"
            @click="ask"
          >
            带引用回答
          </button>
        </div>
        <p class="text-xs text-slate-500">
          先检索原文，再调用当前 AI 接口。没有依据时会提示无法确定。
        </p>
        <div v-if="answer" class="bg-blue-50/50 rounded-xl p-4 space-y-3">
          <p class="text-sm whitespace-pre-wrap">{{ answer.answer }}</p>
          <details v-if="answer.sources.length">
            <summary class="text-xs cursor-pointer">
              核对引用原文（{{ answer.sources.length }}）
            </summary>
            <div
              v-for="s in answer.sources"
              :key="s.id"
              class="text-xs py-2 border-b"
            >
              <b>[片段{{ s.id }}] </b
              ><a
                v-if="jump(s)"
                :href="jump(s)!"
                target="_blank"
                rel="noopener noreferrer"
                class="text-blue-600"
                >{{ timestamp(s.source_start ?? s.start) }} ↗</a
              >
              <p class="mt-1">{{ s.text }}</p>
            </div>
          </details>
        </div>
      </div>
      <details class="border-t pt-3">
        <summary class="font-medium text-sm cursor-pointer">
          版本历史与执行记录
        </summary>
        <div class="mt-3 space-y-3">
          <select v-model="selectedVersion" class="input">
            <option value="">选择历史版本</option>
            <option v-for="v in details.versions" :key="v.id" :value="v.id">
              {{ v.kind === "summary" ? "总结" : "原文" }} ·
              {{ new Date(v.created * 1000).toLocaleString() }}
            </option></select
          ><template v-if="version">
            <pre
              class="max-h-60 overflow-auto whitespace-pre-wrap text-xs bg-slate-50 p-3"
              >{{ version.content }}</pre>
            <button
              class="btn"
              :disabled="busy || active"
              @click="
                run(
                  () => wb.post(`/tasks/${task.id}/restore/${version.id}`),
                  '历史版本已恢复，恢复前内容也已备份',
                )
              "
            >
              恢复此版本
            </button></template
          >
          <p
            v-for="(e, i) in details.events"
            :key="i"
            class="text-xs text-slate-500"
          >
            {{ new Date(e.created * 1000).toLocaleString() }} · {{ e.stage }} ·
            {{ e.message }}
          </p>
        </div>
      </details>
    </div>
  </section>
</template>
<style scoped>
.btn {
  @apply px-3 py-2 text-xs border rounded-lg bg-white hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed;
}
.input {
  @apply w-full border border-slate-200 rounded-lg bg-white px-3 py-2 text-sm;
}
</style>
