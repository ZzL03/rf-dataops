import { createApp } from "vue";
import {
  ElAlert,
  ElButton,
  ElCheckbox,
  ElInput,
  ElOption,
  ElPagination,
  ElProgress,
  ElSelect,
  ElTable,
  ElTableColumn,
  ElTag,
} from "element-plus";
import "element-plus/dist/index.css";
import App from "./App.vue";
import "./style.css";
import "./management.css";

const app = createApp(App);
for (const component of [
  ElAlert,
  ElButton,
  ElCheckbox,
  ElInput,
  ElOption,
  ElPagination,
  ElProgress,
  ElSelect,
  ElTable,
  ElTableColumn,
  ElTag,
])
  app.component(component.name, component);
app.mount("#app");
