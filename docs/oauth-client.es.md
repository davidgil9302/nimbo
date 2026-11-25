# Crea un cliente OAuth de escritorio en Google

[Read in English](oauth-client.md) · [Volver al README](../README.es.md)

nimbo usa un cliente OAuth de escritorio de Google que tú creas para tu cuenta.
Necesitas un proyecto de Google Cloud con la API de Google Drive habilitada.
Para esta configuración no hace falta una cuenta de facturación ni una tarjeta.

## 1. Crea un proyecto y habilita la API de Drive

1. Abre la [consola de Google Cloud](https://console.cloud.google.com/), crea
   un proyecto y selecciónalo en el selector de proyectos.
2. Ve a **APIs y servicios → Biblioteca**, busca **Google Drive API** y elige
   **Habilitar**.

## 2. Configura y publica la pantalla de consentimiento

1. Abre **Google Auth Platform** y selecciona **Comenzar** si aparece esa
   opción. En **Marca** (*Branding*), escribe un nombre de aplicación que
   reconozcas e indica las direcciones de correo de asistencia al usuario y de
   contacto del desarrollador.
2. En **Público** (*Audience*), elige **Externo**. En **Acceso a los datos**
   (*Data Access*), añade el permiso de Google Drive
   `https://www.googleapis.com/auth/drive`. nimbo lo usa para gestionar el
   espejo en una carpeta existente y trabajar con elementos que el cliente
   OAuth no creó. Consulta la [política de privacidad](../site/privacy.html) (en inglés).
3. En **Marca**, indica la URL de la **página principal** y la URL de la
   **política de privacidad** de la aplicación en un **dominio autorizado**.
   Puedes usar un sitio gratuito de GitHub Pages en un repositorio público.
   Publica una página de inicio que explique la aplicación y enlace a la
   política, publica la política en su propia URL HTTPS y añade el dominio
   del sitio en **Dominios autorizados**. Introduce las dos URL publicadas
   en los campos correspondientes.
4. En **Público**, elige **Publicar aplicación** y comprueba que el estado
   indique **En producción**. En **Pruebas** (*Testing*), los tokens de
   actualización de las aplicaciones que piden el permiso de Drive caducan
   a los siete días. Eso interrumpe los respaldos desatendidos. Si ya
   conectaste tu cuenta en modo de pruebas, ejecuta `backup conectar`
   después de publicar para autorizarla de nuevo.

Publicar cambia el estado de la aplicación. La verificación es una revisión
distinta que realiza Google. Un cliente personal puede mostrar el aviso de
aplicación no verificada incluso después de publicarlo.

## 3. Crea el cliente de escritorio

En **Google Auth Platform → Clientes**, elige **Crear cliente**. En
**Tipo de aplicación**, selecciona **Aplicación de escritorio**, ponle un
nombre y créalo. La CLI gestiona la redirección del navegador a tu equipo.

Descarga el JSON del cliente y mantenlo en privado. No lo compartas ni lo
incluyas en ningún commit. Sustituye `PATH` por la ruta del archivo descargado.

```sh
chmod 600 "PATH"
backup instalar --client-json "PATH"
```

También puedes ejecutar `backup instalar` e introducir el ID y el secreto
cuando el asistente te los pida. El asistente oculta el secreto mientras
escribes. nimbo guarda las credenciales del cliente de escritorio en un
archivo de configuración con permisos 0600 y el token de actualización en el
llavero de Linux.

## 4. Autoriza tu cuenta de Google

Cuando el asistente abra el navegador, elige la cuenta con acceso de escritura
a la carpeta de destino y revisa el permiso de Drive solicitado.

Si aparece **Google no ha verificado esta aplicación**, el cliente OAuth y el
permiso solicitado no han pasado la revisión de Google. Continúa solo si
reconoces el nombre del cliente que acabas de crear. Elige **Configuración
avanzada**, luego **Ir a tu aplicación (no seguro)**, y concede el
acceso. Si Google bloquea esa opción, revisa la cuenta, el público y el estado
de publicación del proyecto, y las políticas de tu organización sobre acceso
mediante OAuth.

Regresa a la terminal para confirmar la cuenta y el destino y terminar la
configuración. Puedes revocar el acceso desde los
[permisos de tu cuenta de Google](https://myaccount.google.com/permissions).
