// Load web-fonts từ vfonts (Naive UI khuyến nghị) — Inter cho UI + Fira Code
// cho mono. Import ở đây thay vì App.vue để đảm bảo hoisted trước khi Vue
// mount.
import 'vfonts/Lato.css'
import 'vfonts/FiraCode.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'

const app = createApp(App)
app.use(createPinia())
app.mount('#app')
