<script setup>
import { useRouter } from 'vue-router'

const router = useRouter()
</script>

<template>
  <div class="nf-page">
    <el-empty description="页面不存在（404）——链接可能已失效或地址输错了">
      <el-button type="primary" @click="router.push('/')">回到总览</el-button>
    </el-empty>
  </div>
</template>

<style scoped>
.nf-page {
  padding: 60px 0;
}
</style>
