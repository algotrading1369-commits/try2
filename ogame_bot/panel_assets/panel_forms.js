'use strict';
// Every editable control maps to a validated backend setting.
const moduleDefinitions = [
 {id:'construction',name:'Construcción',icon:'▤',help:'Cola por objetivos: completa cada nivel objetivo antes del siguiente. El modo inteligente compara la amortización estimada de las minas e incluye energía solar.',groups:[
 ['Constructor',[
 ['builder.enabled','Constructor habilitado','bool'],['automation.economic_live','Construcción e investigación con recursos reales','bool'],['builder.strategy','Estrategia','select',[['priority','Cola por prioridades'],['roi','Inteligente: rentabilidad estimada (ROI)']]],
 ['builder.auto_solar','Construir solar cuando falte energía','bool'],['builder.minimum_energy','Energía mínima disponible','number',0],['builder.max_solar_level','Nivel solar máximo','number',1],
 ['automation.min_interval_seconds','Revisión mínima (s)','number',1],['automation.max_interval_seconds','Revisión máxima (s)','number',1],['automation.idle_retry_seconds','Espera de reserva si no hay hora fiable (s)','number',30]]],
 ['Modelo de rentabilidad',[
 ['builder.economy_speed','Velocidad económica (vacío = sin definir)','nullable',1],['builder.max_payback_days','Amortización máxima (días)','number',0.01],
 ['builder.resource_weights','Ratio de intercambio Metal : Cristal : Deuterio (ej. 2.5 : 1.5 : 1)','tradeRatio'],['builder.production_multipliers','Multiplicadores de producción de cada recurso','resources'],
 ['builder.temperatures','Temperaturas máximas por ID de planeta (JSON)','json']]],
 ['Reservas compartidas con investigación', [['builder.reserve','Recursos que deben quedar disponibles','resources']]]],goals:'building'},
 {id:'research',name:'Investigación',icon:'⌘',help:'Una investigación a la vez, por orden de objetivos. Se comprueban recursos y colas; los requisitos no se construyen automáticamente fuera de la preparación de sonda.',groups:[['Investigación',[['research.enabled','Investigación habilitada','bool']]]],goals:'research'},
 {id:'expeditions',name:'Expediciones',icon:'↗',help:'Configura una composición manual o calcula la capacidad por top 1. El gestor repone slots libres con una lectura nueva de naves antes de cada envío. Activa el gestor y el envío real solo cuando quieras ejecutar tu configuración.',groups:[
 ['Origen y composición',[
 ['fleet.enabled','Gestor de flotas habilitado','bool'],['fleet.expeditions.enabled','Planificar expediciones','bool'],['fleet.expeditions.live','Enviar expediciones reales','bool'],['fleet.expeditions.composition_mode','Composición','select',[['automatic','Automática por top 1'],['manual','Manual']]],['fleet.expeditions.manual_ships','Naves por expedición manual (0 = no incluir)','ships'],['fleet.expeditions.origins','IDs de planetas de origen, separados por coma','ids'],
 ['fleet.expeditions.cargo_ship','Carguero','cargo'],['fleet.expeditions.pathfinder','Incluir explorador','bool'],['fleet.expeditions.probe','Incluir sonda','bool'],
 ['fleet.expeditions.combat_ship','Nave de acompañamiento','select',[['','Ninguna'],['battleship','Nave de batalla'],['destroyer','Destructor'],['reaper','Segador']]],
 ['fleet.expeditions.holding_hours','Tiempo de permanencia (horas)','number',1],['fleet.expeditions.max_parallel','Expediciones simultáneas máximas','number',1]]],
 ['Cálculo de capacidad',[
 ['fleet.expeditions.top1_source','Fuente del top 1','select',[['public_api','API pública'],['manual','Manual']]],['fleet.expeditions.top1_points','Puntos del top 1 (opcional)','nullable',1],
 ['fleet.expeditions.top1_observed_at','Fecha del dato manual (ISO con zona horaria)','nullableText'],['fleet.expeditions.top1_max_age_seconds','Vigencia del top 1 (s)','number',1],
 ['fleet.expeditions.base_cap_override','Capacidad base manual (opcional)','nullable',1],['fleet.expeditions.economy_speed','Velocidad económica','nullable',1],
 ['fleet.expeditions.player_class','Clase','select',[['discoverer','Descubridor'],['collector','Recolector'],['general','General']]],['fleet.expeditions.resource_bonus_percent','Bonificación de recursos (%)','number',0]]]]},
 {id:'shipyard',name:'Hangar',icon:'⚒',help:'Cola de fabricación por planeta y cantidades totales. Se fabrican lotes según recursos y reservas. Las cantidades confirmadas se conservan al reiniciar y no se repiten cuando las naves salen de misión. Los requisitos se comprueban en el juego; esta cola no los investiga automáticamente.',groups:[
 ['Fabricación',[['shipyard.enabled','Habilitar cola de hangar','bool'],['shipyard.live','Fabricar naves con recursos reales','bool'],['shipyard.max_batch','Máximo de naves por lote','number',1]]],['Preparación de sonda',[
 ['automation.probe_target','Preparar una sonda y después continuar con mis objetivos','probe'],['automation.probe_bootstrap_live','Usar recursos reales para los requisitos y una sonda','bool'],['automation.probe_body_id','ID del planeta de fabricación','nullable',1]]]]},
 {id:'defense',name:'Defensa',icon:'◇',help:'Monitorización de amenazas y frecuencia de revisión. La detección de ataques y la evacuación son pasos distintos; el resultado y los bloqueos se muestran debajo.',groups:[
 ['Monitorización',[
 ['defender.enabled','Monitorizar ataques','bool'],['defender.origins','IDs de cuerpos vigilados (vacío = los que admita el planificador)','ids'],['defender.pause_on_unknown','Pausar rutinas si el estado es desconocido','bool'],
 ['defender.trigger_seconds','Umbral de evacuación antes del impacto (s)','number',1],['defender.minimum_lead_seconds','Margen mínimo de reacción (s)','number',1],['defender.max_state_age_seconds','Antigüedad máxima de datos defensivos (s)','number',1]]],
 ['Frecuencia',[
 ['defender.idle_min_seconds','Revisión sin alerta, mínimo (s)','number',1],['defender.idle_max_seconds','Revisión sin alerta, máximo (s)','number',1],
 ['defender.alert_min_seconds','Revisión con alerta, mínimo (s)','number',1],['defender.alert_max_seconds','Revisión con alerta, máximo (s)','number',1]]],
 ['Notificaciones',[
 ['telegram.enabled','Alertas Telegram (credenciales en variables de entorno)','bool'],['telegram.timeout_seconds','Tiempo de espera Telegram (s)','number',1],['telegram.cooldown_seconds','Intervalo entre alertas repetidas (s)','number',1]]]]},
 {id:'fleetsave',name:'FleetSave',icon:'⇄',help:'Evacuación reactiva vinculada al defensor. No hay todavía una salida programada por hora ni cancelación automática. El transporte reúne recursos en un cuerpo propio y no garantiza por sí solo un fleetsave seguro.',groups:[
 ['Evacuación ante ataque',[
 ['defender.destinations','IDs de destinos propios verificados','ids'],['defender.mission','Misión de evacuación','select',[['espionage16','Espionaje a posición 16'],['deployment','Despliegue'],['transport','Transporte']]],
 ['defender.speed_percent','Velocidad defensiva (%) · posición 16 requiere 10','speed'],['defender.minimum_flight_seconds','Duración mínima de ida (s)','number',1],['defender.maximum_flight_seconds','Duración máxima de ida (s)','number',1],
 ['defender.maximum_fuel','Combustible máximo','number',1],['defender.resource_policy','Política de carga','select',[['maximize_value','Maximizar valor transportado'],['require_all','Exigir todos los recursos']]]]],
 ['Reunir recursos por transporte',[
 ['fleet.transports.enabled','Planificar transportes','bool'],['fleet.transports.sources','IDs de orígenes','ids'],['fleet.transports.destination_id','ID del destino propio','nullable',1],
 ['fleet.transports.cargo_ship','Carguero','cargo'],['fleet.transports.minimum_utilization_percent','Ocupación mínima (%)','number',1],['fleet.transports.leave_resources','Recursos que se dejan en origen','resources']]]]},
 {id:'scanner',name:'Modo Scanner',icon:'◎',help:'En modo autónomo se hace una lectura inicial y después se lee a demanda. Los intervalos de esta pestaña se usan en Solo scanner; la espera económica se ajusta en Construcción.',groups:[
 ['Lectura de cuenta',[
 ['scanner.enabled','Scanner habilitado','bool'],['scanner.min_interval_seconds','Intervalo mínimo (s)','number',1],['scanner.max_interval_seconds','Intervalo máximo (s)','number',1],
 ['scanner.max_bodies_per_scan','Máximo de cuerpos por lectura','number',1],['scanner.max_reading_age_seconds','Vigencia máxima de lecturas (s)','number',1]]],
 ['Reciclaje cercano (planificación)',[
 ['fleet.recycling.enabled','Buscar escombros','bool'],['fleet.recycling.origins','IDs de orígenes','ids'],['fleet.recycling.radius_systems','Radio de sistemas (0–20)','number',0],
 ['fleet.recycling.minimum_debris','Escombros mínimos','number',1],['fleet.recycling.max_systems_per_cycle','Sistemas por ciclo (1–100)','number',1]]]]},
 {id:'settings',name:'Configuración general',icon:'⚙',help:'Puedes guardar instrucciones con el bot activo. Se aplican entre operaciones y conservan copia de respaldo. El canal, idioma y visibilidad del navegador requieren reiniciar. Hangar y Expediciones tienen controles independientes de ejecución real.',groups:[
 ['Gestor de flotas',[
 ['fleet.min_interval_seconds','Intervalo mínimo de flotas (s)','number',1],['fleet.max_interval_seconds','Intervalo máximo de flotas (s)','number',1],
 ['fleet.reserved_slots','Slots que deben quedar libres','number',0],['fleet.deuterium_reserve','Reserva de deuterio de flotas','number',0],['fleet.maximum_fuel','Combustible máximo por envío','number',1],
 ['fleet.maximum_oneway_seconds','Duración máxima de ida (s)','number',1],['fleet.speed_percent','Velocidad de flotas (%)','speed'],['fleet.capacities','Capacidades verificadas por nave (JSON)','json']]],
 ['Sesión y tiempos',[
 ['session.manual_login_timeout_seconds','Plazo para acceso manual (s)','number',1],['session.health_check_seconds','Comprobación pasiva de sesión (s)','number',1],['session.max_reconnect_attempts','Intentos de reconexión','number',1],
 ['session.initial_backoff_seconds','Espera inicial de reconexión (s)','number',0.1],['session.max_backoff_seconds','Espera máxima de reconexión (s)','number',0.1],
 ['timing.min_delay_seconds','Pausa mínima entre acciones (s)','number',0],['timing.max_delay_seconds','Pausa máxima entre acciones (s)','number',0]]],
 ['Navegador',[
 ['browser.headless','Ocultar navegador (requiere sesión válida)','bool'],['browser.channel','Navegador','select',[['','Chromium de Playwright'],['chrome','Chrome'],['msedge','Edge'],['chromium','Chromium']]],
 ['browser.locale','Idioma del navegador','text'],['browser.navigation_timeout_seconds','Tiempo máximo de navegación (s)','number',1],['browser.action_timeout_seconds','Tiempo máximo de acción (s)','number',0.1]]]]}
];
const editableFields=[];
function weightsToTradeRatio(weights){const base=weights.deuterium;return Object.fromEntries(Object.entries(weights).map(([key,value])=>[key,Number((base/value).toPrecision(12))]));}
function tradeRatioToWeights(ratio){if(Object.values(ratio).some(v=>!Number.isFinite(v)||v<=0))throw new Error('El ratio debe contener tres números mayores que cero.');return Object.fromEntries(Object.entries(ratio).map(([key,value])=>[key,ratio.metal/value]));}
function readPath(root,path){return path.split('.').reduce((v,k)=>v[k],root);}
function writePath(root,path,value){const keys=path.split('.');let obj=root;for(const key of keys.slice(0,-1))obj=obj[key];obj[keys.at(-1)]=value;}
function fieldControl(def){const [path,label,type,extra]=def;const wrap=document.createElement('label');wrap.textContent=label;let input;
 if(type==='ships'){const group=document.createElement('div');group.className='two-col';for(const [key,name] of [['small_cargo','Carguera pequeña'],['large_cargo','Carguera grande'],['espionage_probe','Sonda'],['pathfinder','Explorador'],['light_fighter','Cazador ligero'],['heavy_fighter','Cazador pesado'],['cruiser','Crucero'],['battleship','Nave de batalla'],['battlecruiser','Acorazado'],['destroyer','Destructor'],['bomber','Bombardero'],['reaper','Segador'],['colony_ship','Colonizador'],['recycler','Reciclador'],['deathstar','Estrella de la muerte']])group.append(fieldControl([path+'.'+key,name,'number',0]));wrap.append(group);return wrap;}
 if(type==='resources'||type==='tradeRatio'){const group=document.createElement('div');group.className='reserve-row';for(const [key,name] of [['metal','Metal'],['crystal','Cristal'],['deuterium','Deuterio']])group.append(fieldControl([path+'.'+key,name,'number',type==='tradeRatio'?0.000001:0]));wrap.append(group);return wrap;}
 input=document.createElement(type==='select'||type==='cargo'||type==='speed'?'select':type==='json'?'textarea':'input');input.id='field-'+path.replaceAll('.','-');
 if(type==='select'||type==='cargo'||type==='speed'){const options=type==='cargo'?[['small_cargo','Nave pequeña de carga'],['large_cargo','Nave grande de carga']]:type==='speed'?Array.from({length:10},(_,i)=>[String((i+1)*10),`${(i+1)*10}%`]):extra;for(const [value,name] of options){const option=document.createElement('option');option.value=value;option.textContent=name;input.append(option);}}
 else if(type==='bool'||type==='probe'){input.type='checkbox';wrap.className='toggle';}
 else if(type==='number'||type==='nullable'){input.type='number';input.min=String(extra??0);input.step='any';}
 else if(type==='json'){input.rows=5;input.spellcheck=false;}
 else input.type='text';
 input.setAttribute('aria-label',label);editableFields.push({path,type,input,label});wrap.append(input);return wrap;
}
function mountModules(){const nav=document.querySelector('nav');const logsButton=nav.querySelector('[data-view="logs"]');for(const mod of moduleDefinitions){const button=document.createElement('button');button.className='nav';button.dataset.view=mod.id;button.textContent=mod.icon+' '+mod.name;nav.insertBefore(button,logsButton);const view=document.createElement('section');view.id=mod.id;view.className='view hidden';const heading=document.createElement('h1');heading.textContent=mod.name;const help=document.createElement('p');help.textContent=mod.help;view.append(heading,help);const grid=document.createElement('div');grid.className='two-col';for(const [name,fields] of mod.groups){const card=document.createElement('article');card.className='card form-card';const title=document.createElement('h3');title.textContent=name;card.append(title);for(const field of fields)card.append(fieldControl(field));grid.append(card);}view.append(grid);
 if(mod.goals){const summary=document.createElement('article');summary.className='card orders-card';const title=document.createElement('h3');title.textContent='Decisión y motivos de espera';const output=document.createElement('pre');output.id='decision-'+mod.id;output.className='module-report';summary.append(title,output);view.append(summary);}if(mod.goals){const card=document.createElement('article');card.className='card form-card orders-card';const title=document.createElement('h3');title.textContent=mod.goals==='building'?'Cola de edificios / límites de minas para ROI':'Cola de investigaciones';const note=document.createElement('p');note.textContent='Añade objetivos y cambia su orden con las flechas. Los objetivos ya alcanzados se omiten. En ROI solo se admiten minas; su orden no determina la rentabilidad.';const list=document.createElement('div');list.id=mod.goals==='building'?'buildingGoals':'researchGoals';list.className='goal-list';const add=document.createElement('button');add.id=mod.goals==='building'?'addBuilding':'addResearch';add.type='button';add.className='button small';add.textContent='+ Añadir objetivo';card.append(title,note,list,add);view.append(card);}
 if(mod.id==='shipyard'){const card=document.createElement('article');card.className='card form-card orders-card';const title=document.createElement('h3');title.textContent='Cola de fabricación';const note=document.createElement('p');note.textContent='Selecciona la nave, el planeta y cuántas unidades fabricar en total. Puedes aumentar la cantidad de una fila existente. Para una orden independiente añade una fila nueva.';const list=document.createElement('div');list.id='shipyardGoals';list.className='goal-list';const add=document.createElement('button');add.id='addShipyard';add.type='button';add.className='button small';add.textContent='+ Añadir fabricación';card.append(title,note,list,add);view.append(card);}
 const status=document.createElement('article');status.className='card orders-card';const title=document.createElement('h3');title.textContent='Última lectura y decisión';const output=document.createElement('pre');output.id='report-'+mod.id;output.className='module-report';status.append(title,output);view.append(status);document.getElementById('settingsFields').append(view);}}
mountModules();
