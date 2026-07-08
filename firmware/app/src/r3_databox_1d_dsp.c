
#include "mmw_ctrl.h"
#include "r3_databox_debug_tool.h"
#include "mmw_point_cloud_psic_lib.h"
#include "mmw_app_pointcloud.h"
#include "mmw_alg_pointcloud.h"
#include "mmw_alg_debug.h"
#include "mmw_type.h"
#include "log.h"
#include "r3_databox_msg_handler.h"

__sram_text int r3_databox_1d_frame_cb(void *mmw_data, void *arg) {
	const MmwPointCloudUserCfg_t *ptr_mmw_user_cfg_const = mmw_point_cloud_get_user_cfg_const();
	/* Skip default 2 frames to wait for clutter coverage */
	if (!r3_databox_obtain_skip_frame_status()) { /* return vaule of 0 indicates that the invalid data frames has skipped */
		r3_databox_upload_zeros_dop_bin_data();	/*	report the 1d fft data of all ants	*/
		if ((ptr_mmw_user_cfg_const->mmw_point_cloud_detection_config.clutter_rm_method == POINT_CLUTTER_REMOVAL_DC)
			&& (!ptr_mmw_user_cfg_const->mmw_point_cloud_detection_config.clutter_halt_en)) {
			mmw_psic_dc_suppression_update();
		}
	}else {
		r3_databox_update_skip_frame_status(); /* count skipped frames */
		/* when clutter_rm_method is DC,skip the last frame init DC */
		if (ptr_mmw_user_cfg_const->mmw_point_cloud_detection_config.clutter_rm_method == POINT_CLUTTER_REMOVAL_DC && !r3_databox_obtain_skip_frame_status()) {
            mmw_clutter_halt_set(MMW_HALT_CLUTTER_UPDATE_ENABLE);
            mmw_psic_dc_suppression_init();
			if (!ptr_mmw_user_cfg_const->mmw_point_cloud_detection_config.clutter_halt_en) {
				mmw_psic_dc_suppression_update(); /* mmw_psic_dc_suppression_init will clear cluuter and needs to be update once */
			}
        }
	}

	/* auto gain function must be cleared manually as soon as gain factor is not used,
	   to make auto gain in next frame works well.
	   Hardware reuired gain factor cleared before chirp transmit period
	 */
    mmw_psic_auto_gain_clear();
    return 0;
}

