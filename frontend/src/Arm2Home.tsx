import { useApp } from './App';
import { hasPermission } from './permissions';
import Workspace from './arm2/Workspace';
import './arm2/arm2.css';

export default function Arm2Home() {
  const {user} = useApp();
  return <Workspace can={permission => hasPermission(user, permission)}/>;
}
