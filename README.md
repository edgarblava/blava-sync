# Blava Sync

Application mobile (PWA) qui affiche un QR code horodaté, changeant 10 fois par seconde.
Les caméras Blava le lisent pendant qu'elles filment pour recaler leur horloge.

Format du QR : `KS1:<heure UTC du téléphone en millisecondes>` (tranches de 100 ms).

Installation : ouvrir le lien, puis « Installer l'application » (Android) ou Partager → « Sur l'écran d'accueil » (iPhone).
Fonctionne hors ligne après la première ouverture.

Mise à jour : modifier les fichiers puis incrémenter `VERSION` dans `sw.js`.
