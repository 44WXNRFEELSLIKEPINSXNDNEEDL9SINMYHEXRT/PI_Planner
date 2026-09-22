import { Shell } from './components/layout/Shell'
import { useHashRoute } from './hooks/useHashRoute'
import { UploadScreen } from './screens/upload/UploadScreen'
import { PlanScreen } from './screens/plan/PlanScreen'
import { RisksScreen } from './screens/risks/RisksScreen'
import { StarMapScreen } from './screens/starmap/StarMapScreen'
import { KpiScreen } from './screens/kpi/KpiScreen'
import { RolesScreen } from './screens/roles/RolesScreen'
import { ProfilesScreen } from './screens/profiles/ProfilesScreen'

export default function App() {
  const [screen, go] = useHashRoute()

  return (
    <Shell screen={screen} onNavigate={go}>
      {screen === 'upload' && <UploadScreen onOpenPlan={() => go('plan')} />}
      {screen === 'plan' && <PlanScreen />}
      {screen === 'risks' && <RisksScreen />}
      {screen === 'starmap' && <StarMapScreen />}
      {screen === 'kpi' && <KpiScreen />}
      {screen === 'roles' && <RolesScreen />}
      {screen === 'profiles' && <ProfilesScreen />}
    </Shell>
  )
}
