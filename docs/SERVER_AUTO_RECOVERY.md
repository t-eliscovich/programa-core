# Server auto-recovery — para que el agente no te deje colgada

**TMT 2026-06-03** — diseñado después de que el server quedó 502 por +20 min y yo no pude reiniciarlo solo. Solución: que AWS lo reinicie automáticamente cuando se cuelga, sin depender de credenciales en el sandbox del agente.

---

## TL;DR

**Pegá esto en CloudShell hoy. Una sola vez. Tarda 30 segundos. Después no me dejás más colgada cuando el server hangea.**

```bash
aws cloudwatch put-metric-alarm \
  --region us-east-2 \
  --alarm-name "intela-ec2-auto-reboot-instance-fail" \
  --alarm-description "Reinicia automaticamente el EC2 si el instance check falla 2 minutos seguidos (VM colgada, Waitress hung, etc.). Activa al detectar StatusCheckFailed_Instance, dispara ec2:reboot via la accion automate." \
  --metric-name StatusCheckFailed_Instance \
  --namespace AWS/EC2 \
  --statistic Maximum \
  --period 60 \
  --evaluation-periods 2 \
  --threshold 0 \
  --comparison-operator GreaterThanThreshold \
  --dimensions Name=InstanceId,Value=i-0fcca4d7029f08489 \
  --alarm-actions arn:aws:automate:us-east-2:ec2:reboot \
  --treat-missing-data missing
```

Mientras estés en CloudShell, aprovechá y ponele también el recovery transparente para fallas del hardware AWS (cero costo, AWS migra la VM solo):

```bash
aws cloudwatch put-metric-alarm \
  --region us-east-2 \
  --alarm-name "intela-ec2-auto-recover-system-fail" \
  --alarm-description "Recovery transparente cuando AWS detecta hardware failure en el host (StatusCheckFailed_System)." \
  --metric-name StatusCheckFailed_System \
  --namespace AWS/EC2 \
  --statistic Maximum \
  --period 60 \
  --evaluation-periods 2 \
  --threshold 0 \
  --comparison-operator GreaterThanThreshold \
  --dimensions Name=InstanceId,Value=i-0fcca4d7029f08489 \
  --alarm-actions arn:aws:automate:us-east-2:ec2:recover \
  --treat-missing-data missing
```

**Costo total**: ~$0.20/mes (2 alarmas estándar de CloudWatch). Cero credenciales nuevas en ningún lado.

Verificá que quedaron creadas:
```bash
aws cloudwatch describe-alarms --region us-east-2 --alarm-names \
  intela-ec2-auto-reboot-instance-fail intela-ec2-auto-recover-system-fail \
  --query "MetricAlarms[].[AlarmName,StateValue,ActionsEnabled]" --output table
```

---

## Por qué esta es la mejor opción para vos

Tu prioridad (de la skill `intela-aws-deploy`): "no manejar credenciales AWS en la laptop". Esta solución respeta eso 100% — todo vive dentro de AWS.

| Criterio | Por qué esta gana |
|---|---|
| **Credenciales** | Cero credenciales nuevas. La acción `automate:ec2:reboot/recover` corre dentro de AWS, no necesita IAM user ni access keys en ningún lado. |
| **Costo** | ~$0.20/mes (2 alarmas estándar). Nada más. |
| **Setup** | Una sola línea de CLI en CloudShell. Sin Lambda, sin EventBridge, sin Terraform, sin gestionar IAM policies. |
| **Mantenimiento** | Cero. Las alarmas viven en AWS, no hay código que se rompa. |
| **Tiempo de detección** | ~2-3 minutos (2 períodos × 60s + delay de evaluación). Tolerable para un app interno. |
| **Cobertura** | StatusCheckFailed_Instance dispara cuando: la VM no responde a network, Windows no bootea, o el OS está hung. Captura el caso de hoy. |

---

## Cómo cubre el escenario que pasó hoy

Hoy el server quedó en estado donde:
- HTTPS retornaba 502 después de un restart
- La Scheduled Task NO logró bringback el proceso
- Connection refused → instance health check eventualmente falla
- Con la alarma activa: detecta 2 ciclos seguidos de `StatusCheckFailed_Instance` → dispara reboot → VM arranca limpia → Scheduled Task lanza el app normal

Total downtime con auto-recovery: ~5 min (2 min detección + 3 min reboot). Sin auto-recovery: hasta que alguien lo note y reaccione (hoy fueron 20+ min y contando).

---

## Alternativas consideradas (y por qué quedaron afuera)

### B. Watchdog interno en otra VM
- ❌ Otra VM con su mantenimiento. Otro punto de falla.
- ❌ Necesita IAM creds en algún lado.
- ❌ ~$10/mes mínimo (t4g.nano).

### C. IAM user con credenciales en `.env` del sandbox
**Esta sería la 2ª mejor opción si necesitás que el agente HAGA OTRAS COSAS** (no solo reboot, también describe-instances, SSM Run Command, etc).

Setup mínimo si decidís ir por acá:

```bash
# 1. Crear user
aws iam create-user --user-name programa-core-agent

# 2. Crear policy minima
cat > /tmp/agent-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus",
        "ec2:RebootInstances"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "ec2:Region": "us-east-2"
        }
      }
    },
    {
      "Effect": "Allow",
      "Action": ["ssm:SendCommand", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations"],
      "Resource": [
        "arn:aws:ec2:us-east-2:743978811761:instance/i-0fcca4d7029f08489",
        "arn:aws:ssm:us-east-2::document/AWS-RunPowerShellScript"
      ]
    }
  ]
}
EOF
aws iam put-user-policy --user-name programa-core-agent \
  --policy-name programa-core-agent-policy \
  --policy-document file:///tmp/agent-policy.json

# 3. Crear access key
aws iam create-access-key --user-name programa-core-agent
```

Después pegás `AccessKeyId` y `SecretAccessKey` en el `.env` del proyecto (`/Users/tamaraeliscovich/Documents/Claude/Projects/Programa Core/.env`) como:
```
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-2
INTELA_EC2_INSTANCE_ID=i-0fcca4d7029f08489
```

**Pros**: yo tengo control programático completo (deploy, restart, debug SSM, queries DB via SSM).
**Cons**: una credencial más que rotar, secret que podría leak en logs si yo la imprimo. Lo evitamos por defecto.

**Recomendación**: implementá A primero. Si después necesitamos más capacidad agéntica, agregamos C como complemento.

### D. Lambda + EventBridge cron
- ❌ Más infra (Lambda function, IAM role, EventBridge rule).
- ❌ Si el ping cron pega a `/_healthz` y falla 3 veces → SSM restart. Más reactivo que A (puede detectar app crash sin StatusCheckFailed) PERO requiere endpoint healthz + más mantenimiento.
- 🟡 Mejor opción si querés detección a nivel APP (Python crash con Waitress aún listening). Equivalente a la opción `route53 health check` pero más complejo.

---

## Mejora futura: app-level health check

Cuando vuelva el server, deployo un endpoint nuevo `/_healthz` que:
- Responde 200 si el proceso vive Y el pool de DB responde a `SELECT 1` en <2s
- Responde 503 si DB pool stuck (catch antes del crash visible)
- Sin auth, lightweight, sin DB write

Combinado con una Route53 Health Check (~$0.50/mes) gratis los primeros 3 endpoints, podés tener detección a nivel app. Pero no es necesario ahora — la alarma sobre `StatusCheckFailed_Instance` ya cubre el caso del 502 de hoy.

Lo dejo deployado como base, vos decidís si activás Route53 después.

---

## Setup en una sesión: pasos exactos para mañana

1. Abrir https://us-east-2.console.aws.amazon.com/cloudshell/home?region=us-east-2
2. Esperar que CloudShell cargue (15-20s la primera vez)
3. Copiar y pegar el bloque TL;DR de arriba (los 2 `aws cloudwatch put-metric-alarm`)
4. Verificar con el `describe-alarms`
5. Cerrar CloudShell. Listo. No tocás más nunca.

Después de eso, si el server vuelve a colgar, la próxima vez yo no necesito molestarte — AWS lo reinicia solo en ~3 minutos.

---

## Verificación

Después de aplicar, podés simular un fail para probar:

```bash
# Esto envía el alarma a estado ALARM manualmente para verificar que la action dispara.
aws cloudwatch set-alarm-state --region us-east-2 \
  --alarm-name intela-ec2-auto-reboot-instance-fail \
  --state-value ALARM \
  --state-reason "Test manual disparo accion automate:reboot"
```

(Ojo: SI corres este test, el server SE VA A REINICIAR de verdad. Solo testealo si querés validar que la cadena funciona end-to-end. Después la alarma vuelve a OK sola.)

---

## Resumen para el próximo agente

Si en una sesión futura el server está down y vos sos el agente:

1. Espera ~3 minutos. La alarma auto debería reiniciar.
2. Si después de 5 min sigue down, el problema es OS-level (corrupted state, disk full, etc.) — necesita reboot duro manual.
3. Si el problema es app-level (Python crash en startup), el reboot no ayuda — necesita git revert del último commit o intervención de Tamara/Federico.

Pero el caso típico (Waitress hung, VM unresponsive) queda cubierto.
