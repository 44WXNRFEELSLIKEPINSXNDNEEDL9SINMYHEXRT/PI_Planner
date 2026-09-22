import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { MantineProvider } from '@mantine/core'
import { Notifications } from '@mantine/notifications'
import { QueryClientProvider } from '@tanstack/react-query'
import '@mantine/core/styles.css'
import '@mantine/notifications/styles.css'
import '@mantine/dropzone/styles.css'
import './theme/tokens.css'
import { theme } from './theme/mantineTheme'
import { queryClient } from './api/queryClient'
import { RunProvider } from './hooks/useRun'
import App from './App'

const container = document.getElementById('root')
if (!container) throw new Error('в index.html нет #root')

createRoot(container).render(
  <StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="light">
      <Notifications position="top-right" />
      <QueryClientProvider client={queryClient}>
        <RunProvider>
          <App />
        </RunProvider>
      </QueryClientProvider>
    </MantineProvider>
  </StrictMode>,
)
