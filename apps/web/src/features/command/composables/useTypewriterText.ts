import { computed, onBeforeUnmount, ref, watch, type Ref } from "vue";

const FRAME_DELAY_MS = 28;

/**
 * 只平滑展示后端已经交付的文本，不生成或推断任何 Runtime 状态。
 * 积压较多时会自适应加速，避免长回复在 Run 完成后仍播放过久。
 */
export function useTypewriterText(
  target: Ref<string>,
  identity: Ref<string | null>,
  animate: Ref<boolean> = ref(true),
) {
  const displayedText = ref("");
  let timer: ReturnType<typeof setTimeout> | undefined;

  const prefersReducedMotion = () =>
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function stop() {
    if (timer !== undefined) {
      clearTimeout(timer);
      timer = undefined;
    }
  }

  function schedule() {
    if (!animate.value) {
      displayedText.value = target.value;
      return;
    }
    if (timer !== undefined || displayedText.value === target.value) return;
    timer = setTimeout(tick, FRAME_DELAY_MS);
  }

  function tick() {
    timer = undefined;
    const nextTarget = target.value;

    // 最终持久化文本若修正了临时流内容，以最终真源为准，避免拼接错误。
    if (!nextTarget.startsWith(displayedText.value)) {
      displayedText.value = nextTarget;
      return;
    }

    if (prefersReducedMotion()) {
      displayedText.value = nextTarget;
      return;
    }

    const displayedLength = Array.from(displayedText.value).length;
    const targetCharacters = Array.from(nextTarget);
    const backlog = targetCharacters.length - displayedLength;
    const charactersPerFrame = backlog > 600 ? 4 : backlog > 240 ? 2 : 1;
    displayedText.value = targetCharacters
      .slice(0, displayedLength + charactersPerFrame)
      .join("");
    schedule();
  }

  watch(identity, () => {
    stop();
    displayedText.value = animate.value ? "" : target.value;
    schedule();
  });

  watch(animate, (enabled) => {
    stop();
    if (!enabled) {
      displayedText.value = target.value;
      return;
    }
    displayedText.value = "";
    schedule();
  });

  watch(target, (nextTarget) => {
    if (!animate.value) {
      stop();
      displayedText.value = nextTarget;
      return;
    }
    if (!nextTarget) {
      stop();
      displayedText.value = "";
      return;
    }
    schedule();
  }, { immediate: true });

  onBeforeUnmount(stop);

  return {
    displayedText,
    isTyping: computed(() => displayedText.value !== target.value),
  };
}
