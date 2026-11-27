<script setup>
import { CopyDocument } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const props = defineProps({ text: { type: String, default: '' } })

async function copy() {
  const t = props.text
  if (!t) return
  try {
    await navigator.clipboard.writeText(t)
    ElMessage.success('已复制')
  } catch {
    // 降级：非 https 环境 clipboard 不可用
    const ta = document.createElement('textarea')
    ta.value = t
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
    ElMessage.success('已复制')
  }
}
</script>

<template>
  <el-button link type="primary" size="small" title="复制" @click.stop="copy">
    <el-icon :size="13"><CopyDocument /></el-icon>
  </el-button>
</template>
