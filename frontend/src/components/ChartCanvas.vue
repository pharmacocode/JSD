<script setup>
/**
 * Simple Chart.js wrapper — bar / line / doughnut only (spec 7: keep
 * charts simple, not a BI platform).
 */
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import {
  Chart,
  BarController,
  LineController,
  DoughnutController,
  BarElement,
  LineElement,
  PointElement,
  ArcElement,
  CategoryScale,
  LinearScale,
  Tooltip,
  Legend,
} from 'chart.js'

Chart.register(
  BarController,
  LineController,
  DoughnutController,
  BarElement,
  LineElement,
  PointElement,
  ArcElement,
  CategoryScale,
  LinearScale,
  Tooltip,
  Legend
)

const props = defineProps({
  type: { type: String, default: 'bar' }, // bar | line | doughnut
  labels: { type: Array, default: () => [] },
  datasets: { type: Array, default: () => [] },
  height: { type: Number, default: 220 },
})

// Bar/segment clicks drive the Home chart drill-downs (user request):
// (index, label) of the clicked element.
const emit = defineEmits(['select'])

const canvas = ref(null)
let chart = null

const PALETTE = ['#0d47a1', '#00838f', '#1b8a3a', '#e65100', '#6a1b9a', '#c62828']

function build() {
  if (chart) chart.destroy()
  if (!canvas.value) return
  const isDoughnut = props.type === 'doughnut'
  chart = new Chart(canvas.value, {
    type: props.type,
    data: {
      labels: props.labels,
      datasets: props.datasets.map((ds, i) => ({
        backgroundColor: isDoughnut
          ? props.labels.map((_, j) => PALETTE[j % PALETTE.length])
          : PALETTE[i % PALETTE.length] + (props.type === 'line' ? '33' : ''),
        borderColor: isDoughnut
          ? '#fff'
          : PALETTE[i % PALETTE.length],
        borderWidth: isDoughnut ? 2 : 2,
        fill: props.type === 'line',
        tension: 0.3,
        ...ds,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      onHover: (_evt, elements) => {
        if (canvas.value)
          canvas.value.style.cursor = elements.length ? 'pointer' : 'default'
      },
      onClick: (_evt, elements) => {
        if (elements.length)
          emit('select', elements[0].index, props.labels[elements[0].index])
      },
      plugins: { legend: { display: isDoughnut || props.datasets.length > 1 } },
      scales: isDoughnut
        ? {}
        : { y: { beginAtZero: true, ticks: { precision: 0 } } },
    },
  })
}

onMounted(build)
watch(() => [props.labels, props.datasets, props.type], build, { deep: true })
onBeforeUnmount(() => chart && chart.destroy())
</script>

<template>
  <div :style="{ height: height + 'px', position: 'relative' }">
    <canvas ref="canvas" />
  </div>
</template>
