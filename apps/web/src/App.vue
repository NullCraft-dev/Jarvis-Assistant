<script setup lang="ts">
import { NMessageProvider } from "naive-ui";
import AppShell from "@/components/layout/AppShell.vue";
</script>

<template>
  <NMessageProvider>
    <AppShell>
      <router-view />
    </AppShell>
  </NMessageProvider>
</template>
