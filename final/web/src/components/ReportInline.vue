<template>
  <template v-for="(token, index) in tokens" :key="index">
    <a v-if="token.type === 'citation'" :href="`#research-source-${token.sourceId}`" class="citation-link" :aria-label="`查看来源 ${token.sourceId}`" @click.prevent="$emit('citation', token.sourceId)">{{ token.text }}</a>
    <a v-else-if="token.type === 'link'" :href="token.href" target="_blank" rel="noopener noreferrer">{{ token.text }}</a>
    <strong v-else-if="token.type === 'strong'">{{ token.text }}</strong>
    <em v-else-if="token.type === 'em'">{{ token.text }}</em>
    <code v-else-if="token.type === 'code'">{{ token.text }}</code>
    <span v-else>{{ token.text }}</span>
  </template>
</template>
<script setup>
import { computed } from 'vue'
import { reportInlineTokens } from '../utils/research'
const props = defineProps({ text: { type: String, default: '' }, sources: { type: Array, default: () => [] }, references: { type: Array, default: () => [] } })
defineEmits(['citation'])
const tokens = computed(() => reportInlineTokens(props.text, props.sources, props.references))
</script>
