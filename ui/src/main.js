import '@fontsource-variable/geist'
import '@fontsource-variable/geist-mono'
import './tokens.css'
import './app.css'
import { mount } from 'svelte'
import App from './App.svelte'

export default mount(App, { target: document.getElementById('app') })
